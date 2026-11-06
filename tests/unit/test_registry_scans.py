"""The integration reads the registries through its own config entry, unless a function is listed here with its reason.

The registries hold the entries of every integration, and a unique_id is unique per integration only. A scan of a whole
registry that matches on the unique_id can therefore pick another integration's entry, and whatever deletes or changes
the result then damages that integration (found in the HACS inclusion review, fixed in #270: the marker/KNX cleanup
could delete a foreign entity). er.async_entries_for_config_entry / dr.async_entries_for_config_entry see this entry
only. A scan that has to see more goes into ALLOWED with the reason.

A scan is an access to a registry mapping or a use of an HA helper that also returns other integrations' entries
(`async_entries_for_area` and the like): as an attribute, a name (import aliases resolved), or a literal name in
`getattr` / `attrgetter` (dotted paths too).
Each access counts per function and mapping or helper, so a second access added to a listed function fails too, and so
does a listed scan swapped for another one. A name counts on any object, since an alias of the registry looks like any
other name: a false alarm goes into ALLOWED, a missed scan would not.
Not followed: `getattr` with a computed name or bound to another name (`get = getattr`); a mapping or helper bound to a
local variable, which counts once where it is bound, not at each later use; a scan reached through a helper outside the
integration; whether the entry_id passed to async_entries_for_config_entry is this entry's - the integration holds no
other entry; and what a listed function does with what it reads - that is for the review of the diff that changes it.
"""

import ast
from collections import Counter
from pathlib import Path

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"

ENTITIES = "entities"
DELETED_ENTITIES = "deleted_entities"
DEVICES = "devices"
DELETED_DEVICES = "deleted_devices"
REGISTRY_MAPPINGS = frozenset({ENTITIES, DELETED_ENTITIES, DEVICES, DELETED_DEVICES})
# er / dr helpers whose result is not limited to one config entry.
FOREIGN_ENTRY_HELPERS = frozenset(
    {"async_entries_for_area", "async_entries_for_category", "async_entries_for_device", "async_entries_for_label"}
)
SCANNED_NAMES = REGISTRY_MAPPINGS | FOREIGN_ENTRY_HELPERS

ORPHANED_STATISTICS = "ComexioCoordinator.async_detect_orphaned_statistics"

# (file relative to the integration, Class.method, mapping or helper) -> (uses counted there, why that scan is safe).
ALLOWED = {
    ("__init__.py", "async_setup_entry", DELETED_ENTITIES): (
        1,
        "only reads: deleted entries of this platform and this server's offline IOs, their statistics stay protected",
    ),
    ("coordinator.py", ORPHANED_STATISTICS, DELETED_ENTITIES): (
        1,
        "only reads: deleted entries of this platform and this config entry",
    ),
    ("coordinator.py", ORPHANED_STATISTICS, ENTITIES): (
        1,
        "only reads the entity_ids, of all integrations on purpose: a statistic of any live entity is never an orphan",
    ),
}


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """`from x import name as alias` -> {alias: name}, so an alias reads like the name it stands for."""
    return {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname
    }


def _called_name(func: ast.expr, aliases: dict[str, str]) -> str:
    """The plain name a call calls: `operator.attrgetter`, `op.attrgetter` and an alias all read `attrgetter`."""
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    return func.attr if isinstance(func, ast.Attribute) else ""


def _read_names(node: ast.Call, aliases: dict[str, str]) -> list[ast.expr]:
    """The arguments that name an attribute: getattr's second one (not its default), every one of attrgetter's."""
    func = _called_name(node.func, aliases)
    if func == "getattr":
        return node.args[1:2]
    return node.args if func == "attrgetter" else []


def _scanned(node: ast.AST, aliases: dict[str, str]) -> list[str]:
    """The registry mappings and foreign-entry helpers this node reaches."""
    if isinstance(node, ast.Attribute) and node.attr in SCANNED_NAMES:
        return [node.attr]
    if isinstance(node, ast.Name) and aliases.get(node.id, node.id) in FOREIGN_ENTRY_HELPERS:
        return [aliases.get(node.id, node.id)]
    if isinstance(node, ast.Call):
        # A dotted attrgetter path ("entities.values") reaches every part of it.
        paths = (arg.value for arg in _read_names(node, aliases) if isinstance(arg, ast.Constant))
        return [part for path in paths if isinstance(path, str) for part in path.split(".") if part in SCANNED_NAMES]
    return []


def _scan_users(source: str) -> Counter[tuple[str, str]]:
    """Registry scans per (function, mapping or helper): Class.method, nested functions dotted, module level ''."""
    users: Counter[tuple[str, str]] = Counter()
    tree = ast.parse(source)
    aliases = _import_aliases(tree)

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{owner}.{child.name}" if owner else child.name
            else:
                users.update((owner, scanned) for scanned in _scanned(child, aliases))
            visit(child, name)

    visit(tree, "")
    return users


def test_detector_finds_scans_of_every_registry_mapping() -> None:
    # The shape #270 fixed: it must fail, not just the call on .values().
    cleanup = "class C:\n    def find(self, reg):\n        return [e for e in reg.entities.values() if e.unique_id]\n"
    membership = "def live(reg):\n    return set(reg.entities)\n"
    items = "def pairs(reg):\n    return list(reg.entities.items())\n"
    deleted = "def gone(reg):\n    return reg.deleted_entities.values()\n"
    devices = "def hubs(reg):\n    return reg.devices.values()\n"
    deleted_devices = "def old(reg):\n    return reg.deleted_devices.values()\n"
    awaited = "async def wait(reg):\n    return reg.entities\n"
    nested = "def outer(reg):\n    def inner():\n        return reg.entities\n    return inner\n"
    twice = "def two(reg):\n    return reg.entities, reg.entities\n"
    mixed = "def both(reg):\n    return reg.entities, reg.devices\n"
    module_level = "ALL = registry.entities\n"
    own_entry = "def own(reg, entry):\n    return er.async_entries_for_config_entry(reg, entry.entry_id)\n"
    plain_name = "def own(entities):\n    return entities.values()\n"
    dict_key = "def count(full):\n    return full['devices']\n"

    assert _scan_users(cleanup) == {("C.find", ENTITIES): 1}
    assert _scan_users(membership) == {("live", ENTITIES): 1}
    assert _scan_users(items) == {("pairs", ENTITIES): 1}
    assert _scan_users(deleted) == {("gone", DELETED_ENTITIES): 1}
    assert _scan_users(devices) == {("hubs", DEVICES): 1}
    assert _scan_users(deleted_devices) == {("old", DELETED_DEVICES): 1}
    assert _scan_users(awaited) == {("wait", ENTITIES): 1}
    assert _scan_users(nested) == {("outer.inner", ENTITIES): 1}
    assert _scan_users(twice) == {("two", ENTITIES): 2}
    assert _scan_users(mixed) == {("both", ENTITIES): 1, ("both", DEVICES): 1}
    assert _scan_users(module_level) == {("", ENTITIES): 1}
    assert not _scan_users(own_entry)
    assert not _scan_users(plain_name)
    assert not _scan_users(dict_key)


def test_detector_finds_scans_by_name_and_through_helpers() -> None:
    by_name = "def dyn(reg):\n    return getattr(reg, 'entities').values()\n"
    qualified = "def qual(reg):\n    return builtins.getattr(reg, 'devices')\n"
    getter = "def get(reg):\n    return operator.attrgetter('deleted_entities')(reg)\n"
    area = "def room(reg, area):\n    return er.async_entries_for_area(reg, area)\n"
    imported = "def tagged(reg, label):\n    return async_entries_for_label(reg, label)\n"
    getter_of_two = "def pair(reg):\n    return operator.attrgetter('entities', 'devices')(reg)\n"
    plain_import = "from homeassistant.helpers.entity_registry import async_entries_for_area\n"
    imported_and_called = plain_import + "def room(reg, area):\n    return async_entries_for_area(reg, area)\n"
    aliased = (
        "from homeassistant.helpers.entity_registry import async_entries_for_area as area_entries\n"
        "def room(reg, area):\n    return area_entries(reg, area)\n"
    )
    aliased_getter = "from operator import attrgetter as ag\ndef get(reg):\n    return ag('devices')(reg)\n"
    module_alias = "def get(reg):\n    return op.attrgetter('entities')(reg)\n"
    dotted = "def walk(reg):\n    return operator.attrgetter('entities.values')(reg)()\n"
    helper_by_name = "def room(area):\n    return getattr(er, 'async_entries_for_area')(reg, area)\n"
    other_attribute = "def flag(self):\n    return getattr(self.watchdog, 'auto_restart')\n"
    default_value = "def mode(self):\n    return getattr(self, 'mode', 'devices')\n"

    assert _scan_users(by_name) == {("dyn", ENTITIES): 1}
    assert _scan_users(qualified) == {("qual", DEVICES): 1}
    assert _scan_users(getter) == {("get", DELETED_ENTITIES): 1}
    assert _scan_users(getter_of_two) == {("pair", ENTITIES): 1, ("pair", DEVICES): 1}
    assert _scan_users(area) == {("room", "async_entries_for_area"): 1}
    assert _scan_users(imported) == {("tagged", "async_entries_for_label"): 1}
    # The call counts where it happens; an unused import reads nothing.
    assert _scan_users(imported_and_called) == {("room", "async_entries_for_area"): 1}
    assert not _scan_users(plain_import)
    assert _scan_users(aliased) == {("room", "async_entries_for_area"): 1}
    assert _scan_users(aliased_getter) == {("get", DEVICES): 1}
    assert _scan_users(module_alias) == {("get", ENTITIES): 1}
    assert _scan_users(dotted) == {("walk", ENTITIES): 1}
    assert _scan_users(helper_by_name) == {("room", "async_entries_for_area"): 1}
    assert not _scan_users(other_attribute)
    assert not _scan_users(default_value)


def test_every_allowed_scan_says_why() -> None:
    unexplained = sorted(key for key, (count, reason) in ALLOWED.items() if count < 1 or not reason.strip())
    assert not unexplained, f"ALLOWED entries need a use count of at least 1 and a reason: {unexplained}"


def test_every_registry_scan_is_scoped_or_listed() -> None:
    files = sorted(INTEGRATION.rglob("*.py"))
    found = {
        (path.relative_to(INTEGRATION).as_posix(), user, scanned): count
        for path in files
        for (user, scanned), count in _scan_users(path.read_text(encoding="utf-8")).items()
    }
    allowed = {key: count for key, (count, _reason) in ALLOWED.items()}
    unlisted = sorted(f"{key}: {count}" for key, count in found.items() if count > allowed.get(key, 0))
    stale = sorted(f"{key}: {count}" for key, count in allowed.items() if found.get(key, 0) < count)

    # The scan must see the integration it guards, or a moved directory turns it vacuously green.
    assert files
    assert not unlisted, (
        "Whole-registry access - use er/dr.async_entries_for_config_entry, or list it in ALLOWED with the reason "
        f"the scan is safe (file, function, mapping or helper: uses): {unlisted}"
    )
    assert not stale, f"ALLOWED lists more scans than are left - lower the count or remove the entry: {stale}"
