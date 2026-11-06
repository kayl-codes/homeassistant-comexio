"""The integration reads the registries through its own config entry, unless a function is listed here with its reason.

The registries hold the entries of every integration, and a unique_id is unique per integration only. A scan of a whole
registry that matches on the unique_id can therefore pick another integration's entry, and whatever deletes or changes
the result then damages that integration (found in the HACS inclusion review, fixed in #270: the marker/KNX cleanup
could delete a foreign entity). er.async_entries_for_config_entry / dr.async_entries_for_config_entry see this entry
only. A scan that has to see more goes into ALLOWED with the reason.

Each use counts per mapping, so a second scan added to a listed function fails too, and so does a listed scan swapped
for another mapping. An attribute of that name counts on any object, since an alias of the registry looks like any other
name: a false alarm goes into ALLOWED, a missed scan would not.
Not followed: `getattr(registry, "entities")`, HA helpers that also return foreign entries (`er.async_entries_for_area`
and the like), and a scan reached through a helper outside the integration.
"""

import ast
from collections import Counter
from pathlib import Path

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
REGISTRY_MAPPINGS = frozenset({"entities", "deleted_entities", "devices", "deleted_devices"})

# (file relative to the integration, Class.method, registry mapping) -> (uses counted there, why that scan is safe).
ALLOWED = {
    ("__init__.py", "async_setup_entry", "deleted_entities"): (
        1,
        "only reads: deleted entries of this platform and this server's offline IOs, their statistics stay protected",
    ),
    ("coordinator.py", "ComexioCoordinator.async_detect_orphaned_statistics", "deleted_entities"): (
        1,
        "only reads: deleted entries of this platform and this config entry",
    ),
    ("coordinator.py", "ComexioCoordinator.async_detect_orphaned_statistics", "entities"): (
        1,
        "only reads the entity_ids, of all integrations on purpose: a statistic of any live entity is never an orphan",
    ),
}


def _scan_users(source: str) -> Counter[tuple[str, str]]:
    """Whole-registry accesses per (function, mapping): Class.method, nested functions dotted, module level ''."""
    users: Counter[tuple[str, str]] = Counter()

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{owner}.{child.name}" if owner else child.name
            elif isinstance(child, ast.Attribute) and child.attr in REGISTRY_MAPPINGS:
                users[owner, child.attr] += 1
            visit(child, name)

    visit(ast.parse(source), "")
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

    assert _scan_users(cleanup) == {("C.find", "entities"): 1}
    assert _scan_users(membership) == {("live", "entities"): 1}
    assert _scan_users(items) == {("pairs", "entities"): 1}
    assert _scan_users(deleted) == {("gone", "deleted_entities"): 1}
    assert _scan_users(devices) == {("hubs", "devices"): 1}
    assert _scan_users(deleted_devices) == {("old", "deleted_devices"): 1}
    assert _scan_users(awaited) == {("wait", "entities"): 1}
    assert _scan_users(nested) == {("outer.inner", "entities"): 1}
    assert _scan_users(twice) == {("two", "entities"): 2}
    assert _scan_users(mixed) == {("both", "entities"): 1, ("both", "devices"): 1}
    assert _scan_users(module_level) == {("", "entities"): 1}
    assert not _scan_users(own_entry)
    assert not _scan_users(plain_name)


def test_every_allowed_scan_says_why() -> None:
    unexplained = sorted(key for key, (count, reason) in ALLOWED.items() if count < 1 or not reason.strip())
    assert not unexplained, f"ALLOWED entries need a use count of at least 1 and a reason: {unexplained}"


def test_every_registry_scan_is_scoped_or_listed() -> None:
    files = sorted(INTEGRATION.rglob("*.py"))
    found = {
        (path.relative_to(INTEGRATION).as_posix(), user, mapping): count
        for path in files
        for (user, mapping), count in _scan_users(path.read_text(encoding="utf-8")).items()
    }
    allowed = {key: count for key, (count, _reason) in ALLOWED.items()}
    unlisted = sorted(f"{key}: {count}" for key, count in found.items() if count > allowed.get(key, 0))
    stale = sorted(f"{key}: {count}" for key, count in allowed.items() if found.get(key, 0) < count)

    # The scan must see the integration it guards, or a moved directory turns it vacuously green.
    assert files
    assert not unlisted, (
        "Whole-registry access - use er/dr.async_entries_for_config_entry, or list it in ALLOWED with the reason "
        f"the scan is safe (file, function, mapping: uses): {unlisted}"
    )
    assert not stale, f"ALLOWED lists more scans than are left - lower the count or remove the entry: {stale}"
