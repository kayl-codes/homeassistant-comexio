"""A test double of the coordinator binds every coordinator method that the real methods it runs call.

Some tests run real `ComexioCoordinator` methods on a stand-in (`class _FakeCoordinator(SimpleNamespace)` with
`some_method = ComexioCoordinator.some_method` in its body). A real method that calls `self.other_method()` needs
`other_method` on the stand-in too. Where it is missing, the test fails with an AttributeError far from its cause
- or, on a path with `except Exception` (a restore, a tick), it ends green for the wrong reason. This happened 20
times (the counter of the open point "Fake-Bindungstest"); every case was one short fix round, so this test
only moves the failure to where the cause is named.

The scan reads every class in `tests/unit` that binds a coordinator method in its body (also through a module-level
alias of the class). For each bound method it takes the `self.<name>` uses in the coordinator source and checks that
every `<name>` that the real coordinator has as a method, classmethod or property (its own or Home Assistant's) is
supplied in that test file: bound or defined in the class body, or given elsewhere in the file - an attribute assigned
(`fake.name = ...`), a `setattr` / `patch.object` with a literal name, or a keyword argument of a call that builds a
double (a call of the double's class, or of a function whose name says coordinator, fake or double). A name supplied
for one test counts for the whole file, so a double that lacks it only in another test is not found.

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
DOUBLE_WORDS = ("coordinator", "fake", "double")

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


def _class_aliases(tree: ast.Module) -> set[str]:
    """The coordinator class under every name the module gives it (`import ... as`, `Alias = ComexioCoordinator`)."""
    aliases = {COORDINATOR}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            aliases.update(alias.asname for alias in statement.names if alias.name == COORDINATOR and alias.asname)
        elif (
            isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Name)
            and statement.value.id in aliases
        ):
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
    if isinstance(value.value, ast.Name) and value.value.id in aliases:
        return target.id, value.attr
    return None


def _defined_in_body(double: ast.ClassDef) -> set[str]:
    """Names the class body defines itself: assignments (annotated ones too) and methods."""
    names: set[str] = set()
    for statement in double.body:
        if isinstance(statement, ast.Assign):
            names.update(target.id for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names.add(statement.target.id)
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(statement.name)
    return names


def _builds_a_double(call: ast.Call, doubles: set[str]) -> bool:
    """A call of a double's class, or of a helper whose name says it builds one (`_coordinator(api, **kwargs)`)."""
    name = ast.unparse(call.func).rsplit(".", 1)[-1]
    return name in doubles or any(word in name.lower() for word in DOUBLE_WORDS)


def _supplied_in_file(tree: ast.AST, doubles: set[str]) -> set[str]:
    """Names a test of the file hands to a double: assigned attributes, setattr, patch.object, keywords at a build."""
    supplied: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _builds_a_double(node, doubles):
                supplied.update(keyword.arg for keyword in node.keywords if keyword.arg)
            if ast.unparse(node.func).endswith(("setattr", "patch.object")) and len(node.args) >= 2:
                name = node.args[1]
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    supplied.add(name.value)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            supplied.update(target.attr for target in targets if isinstance(target, ast.Attribute))
    return supplied


def missing_bindings(
    source: str, methods: dict[str, ast.AST], members: frozenset[str]
) -> dict[tuple[str, str, str], str]:
    """(double class, bound method, missing name) -> coordinator method, for every double of the source."""
    tree = ast.parse(source)
    aliases = _class_aliases(tree)
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    bound_of = {
        double.name: [pair for statement in double.body if (pair := _bound_coordinator_method(statement, aliases))]
        for double in classes
    }
    supplied = _supplied_in_file(tree, {name for name, bound in bound_of.items() if bound})
    found: dict[tuple[str, str, str], str] = {}
    for double in classes:
        bound = bound_of[double.name]
        own = {name for name, _method in bound} | _defined_in_body(double)
        for name, method in bound:
            if method not in methods:
                continue  # inherited from Home Assistant's coordinator: no source here to follow
            for needed in sorted(_self_uses(methods[method]) & members):
                if needed not in own | supplied:
                    found[(double.name, name, needed)] = method
    return found


def _findings() -> dict[tuple[str, str, str, str], str]:
    methods, members = _coordinator_methods(), _real_members()
    found: dict[tuple[str, str, str, str], str] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        if path == Path(__file__):
            continue
        for (double, bound, needed), method in missing_bindings(
            path.read_text(encoding="utf-8"), methods, members
        ).items():
            found[(path.name, double, bound, needed)] = method
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
MEMBERS = frozenset({"run", "helper", "other", "other_property"})
FINDING = {("_Fake", "run", "other"): "run", ("_Fake", "helper", "other_property"): "helper"}


def test_detector_finds_a_method_the_double_lacks() -> None:
    assert missing_bindings(DOUBLE, METHODS, MEMBERS) == FINDING


@pytest.mark.parametrize(
    "supply",
    [
        "    other = ComexioCoordinator.other\n    other_property = None\n",
        "    def other(self): ...\n    other_property = None\n",
        "    other: object = None\n    other_property: int = 1\n",
        "\ndef test_a(fake):\n    fake.other = AsyncMock()\n    fake.other_property = 1\n",
        "\ndef test_a(fake):\n    fake.other: int = 1\n    fake.other_property = 1\n",
        "\ndef test_a():\n    _Fake(other=1, other_property=2)\n",
        "\ndef test_a():\n    _coordinator(api, other=1, other_property=2)\n",
        "\ndef test_a(fake):\n    setattr(fake, 'other', 1)\n    patch.object(fake, 'other_property', 2)\n",
    ],
    ids=[
        "bound",
        "defined",
        "annotated",
        "assigned",
        "annotated attribute",
        "keyword of the class",
        "keyword of a builder",
        "setattr and patch.object",
    ],
)
def test_detector_accepts_every_way_to_supply_a_name(supply: str) -> None:
    assert missing_bindings(DOUBLE + supply, METHODS, MEMBERS) == {}


@pytest.mark.parametrize(
    "elsewhere",
    [
        "\ndef test_a():\n    Other(other=1, other_property=2)\n",
        "\ndef test_a(fake, name):\n    setattr(fake, name, 1)\n    patch.object(fake, name, 2)\n",
    ],
    ids=["keyword of a call that builds no double", "setattr with a computed name"],
)
def test_detector_does_not_take_a_foreign_keyword_or_a_computed_name_for_a_supply(elsewhere: str) -> None:
    assert missing_bindings(DOUBLE + elsewhere, METHODS, MEMBERS) == FINDING


def test_detector_follows_a_binding_through_an_alias_of_the_class() -> None:
    aliased = (
        "Alias = ComexioCoordinator\nclass _Fake(SimpleNamespace):\n    run = Alias.run\n    helper = Alias.helper\n"
    )
    imported = "from x import ComexioCoordinator as Real\nclass _Fake:\n    run = Real.run\n    helper = Real.helper\n"

    assert missing_bindings(aliased, METHODS, MEMBERS) == FINDING
    assert missing_bindings(imported, METHODS, MEMBERS) == FINDING


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


def test_a_listed_finding_is_not_reported_and_a_stale_entry_is() -> None:
    key = ("test_x.py", "_Fake", "run", "other")

    assert problems({key: "run"}, {}) != []
    assert problems({key: "run"}, {key: "no test reaches it"}) == []
    assert len(problems({}, {key: "no test reaches it"})) == 1


def test_detector_ignores_classes_without_bound_methods() -> None:
    assert missing_bindings("class _Plain:\n    run = 1\n", METHODS, MEMBERS) == {}
