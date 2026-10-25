"""Every reload of the entry goes through coordinator.async_reload_entry, unless it is listed here with its reason.

Home Assistant's own reload (config_entries.async_reload / async_schedule_reload) tears the coordinator down and
closes the API session, also in the middle of a sync, a Web-IO range check or a function plan restore.
coordinator.async_reload_entry skips the reload while a sync runs (the sync's closing reload covers it) and otherwise
waits for _sync_lock, running restores and plan edits. A new use of HA's reload is a finding unless the guard cannot
apply there - then it goes into ALLOWED with the reason (found by Sourcery on #219: an unchanged reconfigure scheduled
HA's reload, which could cut into a running sync).

Each use counts, so a second reload added to a listed function fails too. A reference counts like a call (passing
the reload on via `partial(...)` or a callback), and so does the bare name as a string (`getattr(..., "async_reload")`).
A `getattr` on config_entries whose name the scan cannot read counts as well, since it may be a reload. Any other
attribute or string named like a reload counts too: a false alarm goes into ALLOWED, a missed reload would not.
Not followed: a reload reached through a helper outside the integration, and whether a listed reload still sits on
the path its reason describes - that is for the review of the diff that moves it.
"""

import ast
from collections import Counter
from pathlib import Path

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
HA_RELOADS = frozenset({"async_reload", "async_schedule_reload"})

# (file relative to the integration, Class.method) -> (uses of HA's reload there, why HA's reload is right there).
ALLOWED = {
    ("__init__.py", "update_listener"): (1, "only without a coordinator: nothing loaded, no sync to cut into"),
    ("button.py", "ComexioSyncButton._reset_sync_state_and_reload"): (
        1,
        "runs under _sync_lock (_finalize_sync holds it until the reload is done), which the guard would wait for",
    ),
    ("config_flow.py", "ComexioConfigFlow._async_update_and_reload_once"): (
        1,
        "only an entry without a loaded coordinator",
    ),
    ("coordinator.py", "ComexioCoordinator.async_reload_entry"): (1, "the guarded reload itself"),
    ("repairs.py", "ComexioRepairFlow._async_cleanup_then_reload"): (
        1,
        "runs under _sync_lock, which the guard would wait for forever",
    ),
    ("repairs.py", "ComexioRepairFlow.async_step_certificate_changed"): (2, "only an entry that is not loaded"),
}


def _computed_config_entries_getattr(node: ast.Call) -> bool:
    """getattr(<...>config_entries, name) with a name that is not a literal (a literal counts as a string)."""
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and not isinstance(node.args[1], ast.Constant)
        and ast.unparse(node.args[0]).endswith("config_entries")
    )


def _uses_reload(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in HA_RELOADS
    if isinstance(node, ast.Call):
        return _computed_config_entries_getattr(node)
    return isinstance(node, ast.Constant) and node.value in HA_RELOADS


def _reload_users(source: str) -> Counter[str]:
    """Uses of HA's reload per function: Class.method, nested functions dotted, module level ''."""
    users: Counter[str] = Counter()

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{owner}.{child.name}" if owner else child.name
            elif _uses_reload(child):
                users[owner] += 1
            visit(child, name)

    visit(ast.parse(source), "")
    return users


def test_detector_finds_calls_and_references() -> None:
    method = "class Flow:\n    async def step(self):\n        await self.hass.config_entries.async_reload(x)\n"
    scheduled = "def done(hass):\n    hass.config_entries.async_schedule_reload(x)\n"
    nested = "async def outer(hass):\n    async def inner():\n        await hass.config_entries.async_reload(x)\n"
    reference = "def later(hass):\n    hass.async_create_task(partial(hass.config_entries.async_reload, x)())\n"
    by_name = "async def dynamic(hass):\n    await getattr(hass.config_entries, 'async_reload')(x)\n"
    computed = "async def pick(hass, name):\n    await getattr(hass.config_entries, name)(x)\n"
    other_getattr = "def flag(self, name):\n    return getattr(self.watchdog, name)\n"
    twice = (
        "def two(hass):\n    hass.config_entries.async_schedule_reload(x)\n    hass.config_entries.async_reload(x)\n"
    )
    module_level = "RELOAD = config_entries.async_reload\n"
    guarded = "async def ok(coordinator):\n    await coordinator.async_reload_entry('async_reload requested')\n"

    assert _reload_users(method) == {"Flow.step": 1}
    assert _reload_users(scheduled) == {"done": 1}
    assert _reload_users(nested) == {"outer.inner": 1}
    assert _reload_users(reference) == {"later": 1}
    assert _reload_users(by_name) == {"dynamic": 1}
    assert _reload_users(computed) == {"pick": 1}
    assert _reload_users(twice) == {"two": 2}
    assert _reload_users(module_level) == {"": 1}
    assert not _reload_users(guarded)
    assert not _reload_users(other_getattr)


def test_every_allowed_reload_says_why() -> None:
    unexplained = sorted(key for key, (count, reason) in ALLOWED.items() if count < 1 or not reason.strip())
    assert not unexplained, f"ALLOWED entries need a use count of at least 1 and a reason: {unexplained}"


def test_every_ha_reload_is_guarded_or_listed() -> None:
    files = sorted(INTEGRATION.rglob("*.py"))
    found = {
        (path.relative_to(INTEGRATION).as_posix(), user): count
        for path in files
        for user, count in _reload_users(path.read_text(encoding="utf-8")).items()
    }
    allowed = {key: count for key, (count, _reason) in ALLOWED.items()}
    unlisted = sorted(f"{key}: {count}" for key, count in found.items() if count > allowed.get(key, 0))
    stale = sorted(f"{key}: {count}" for key, count in allowed.items() if found.get(key, 0) < count)

    # The scan must see the integration it guards, or a moved directory turns it vacuously green.
    assert files
    assert not unlisted, (
        "HA's reload used outside coordinator.async_reload_entry - use the guarded reload, or list it in ALLOWED "
        f"with the reason the guard cannot apply (file, function: uses): {unlisted}"
    )
    assert not stale, f"ALLOWED lists more reloads than are left - lower the count or remove the entry: {stale}"
