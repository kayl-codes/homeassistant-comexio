"""The webhook ids and the entry secret never reach a log line, a notification or a repair, and the host is bound.

HA's webhook endpoint takes no authentication, so the id in /api/webhook/<id> is the secret of its command (dg): whoever
reads one can forge that command's value. Anything the integration prints - a log line, a persistent notification, the
placeholders of a repair, the text of an exception HA shows and logs - can be read by other HA users or ends up in a
pasted log. The behaviour tests pin the places that were wrong once (a refused id, the preview notification, a path
deviation); this scan makes a new place a finding before a bot or a user has to find it.

What counts as printing: a call of a logging method (debug, info, warning, ... on any receiver), `print`,
`warn_refused`, `persistent_notification.async_create`, the repair creators `async_create_issue` /
`async_create_entry_issue` (their placeholders), and the text of a `raise`. What counts as carrying an id: a name or
attribute in SECRET_NAMES, the form field `Parameter` (a command's path), or a local name assigned from such an
expression in the same function. `redact_webhook_ids(...)` makes its argument safe, so a use under it is fine.

Not followed: an id handed through an attribute or another function, and the texts the aiocomexio library puts into its
own exceptions (they quote Comexio's answer, not our request). Those stay for the review of the diff.

A second check keeps the Comexio host bound to the ledger: `WebhookSecurity(...)` must be given the host of the entry,
otherwise a changed host would keep the old id off although another server may still hold commands on it.
"""

import ast
from collections import Counter
from pathlib import Path

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"

# The only places that hold an id or the secret today: the handler's `webhook_id`, `security.secret`,
# `derive_webhook_id` (coordinator) and the path callables (`command_path`, `WebhookSecurity.path_for`).
SECRET_NAMES = frozenset({"webhook_id", "derive_webhook_id", "command_path", "path_for", "secret"})
FORM_PATH_FIELD = "Parameter"
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"})
PRINTING_CALLS = frozenset({"print", "warn_refused", "async_create", "async_create_issue", "async_create_entry_issue"})
SAFE_WRAPPERS = frozenset({"redact_webhook_ids"})

# (file relative to the integration, Class.method) -> (uses counted there, why the id is not shown there). Each use
# counts, so a second one in a listed function fails too.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {}


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_printing(node: ast.Call) -> bool:
    name = _call_name(node)
    if name in PRINTING_CALLS:
        return True
    return isinstance(node.func, ast.Attribute) and name in LOG_METHODS


def _carries_id(node: ast.AST, tainted: frozenset[str] = frozenset()) -> bool:
    """Whether an expression names the secret, reads a command's path field or a tainted name, outside redaction."""
    if isinstance(node, ast.Call) and _call_name(node) in SAFE_WRAPPERS:
        return False
    if isinstance(node, ast.Name) and (node.id in SECRET_NAMES or node.id in tainted):
        return True
    if isinstance(node, ast.Attribute) and node.attr in SECRET_NAMES:
        return True
    if isinstance(node, ast.Constant) and node.value == FORM_PATH_FIELD:
        return True
    return any(_carries_id(child, tainted) for child in ast.iter_child_nodes(node))


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.tainted: list[set[str]] = [set()]
        self.hits: list[tuple[str, int]] = []

    def _enter(self, node: ast.AST, name: str, *, function: bool) -> None:
        self.scope.append(name)
        if function:
            self.tainted.append(set())
        self.generic_visit(node)
        if function:
            self.tainted.pop()
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name, function=False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name, function=True)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _hit(self, node: ast.AST) -> None:
        self.hits.append((".".join(self.scope), node.lineno))

    def visit_Assign(self, node: ast.Assign) -> None:
        if _carries_id(node.value, frozenset(self.tainted[-1])):
            self.tainted[-1].update(t.id for t in node.targets if isinstance(t, ast.Name))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        tainted = frozenset(self.tainted[-1])
        if _is_printing(node) and any(
            _carries_id(arg, tainted) for arg in (*node.args, *(k.value for k in node.keywords))
        ):
            self._hit(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None and _carries_id(node.exc, frozenset(self.tainted[-1])):
            self._hit(node)
        self.generic_visit(node)


def _scan(source: str) -> list[tuple[str, int]]:
    scan = _Scan()
    scan.visit(ast.parse(source))
    return scan.hits


def test_no_log_line_notification_repair_or_exception_carries_a_webhook_id_or_the_secret() -> None:
    found: Counter[tuple[str, str]] = Counter()
    lines: dict[tuple[str, str], list[int]] = {}
    for path in sorted(INTEGRATION.rglob("*.py")):
        rel = path.relative_to(INTEGRATION).as_posix()
        for scope, line in _scan(path.read_text(encoding="utf-8")):
            found[rel, scope] += 1
            lines.setdefault((rel, scope), []).append(line)

    allowed = Counter({key: count for key, (count, _why) in ALLOWED.items()})
    unlisted = {f"{k[0]}::{k[1] or '<module>'} (lines {lines[k]})": n for k, n in (found - allowed).items()}
    stale = {f"{k[0]}::{k[1] or '<module>'}": f"{n} listed too many" for k, n in (allowed - found).items()}
    assert not unlisted, f"a webhook id or the secret may reach a log, a notification or an exception: {unlisted}"
    assert not stale, f"ALLOWED lists uses that no longer exist (lower the count or drop the entry): {stale}"


def test_the_scan_sees_an_id_where_it_is_printed_and_not_where_it_is_redacted_or_not_printed() -> None:
    source = """
class C:
    def leaks(self, webhook_id, secret, cmd):
        _LOGGER.warning("id %s", webhook_id)
        self._logger.debug("secret %s", self.secret)
        logging.getLogger(__name__).info("id %s", webhook_id)
        persistent_notification.async_create(hass, f"path {cmd['Parameter']}")
        async_create_entry_issue(hass, entry, "x", placeholders={"p": derive_webhook_id(secret, "k")})
        security.warn_refused("k", "refused %s", path_for(cls, item))
        url = f"/api/webhook/{webhook_id}"
        _LOGGER.error("url %s", url)
        raise ValueError(f"bad {webhook_id}")

    async def async_leaks(self, webhook_id):
        print(webhook_id)

    def safe(self, text, cmd, webhook_id):
        persistent_notification.async_create(hass, redact_webhook_ids(text))
        _LOGGER.warning("name %s", cmd.get("Name"))
        api.register(webhook_id)
        url = f"/api/other/{cmd}"
        _LOGGER.info("url %s", url)
        raise ValueError("bad value")
"""
    scopes = Counter(scope for scope, _line in _scan(source))

    assert scopes == {"C.leaks": 8, "C.async_leaks": 1}


def test_every_webhook_security_is_built_with_the_host_of_its_entry() -> None:
    built = 0
    for path in sorted(INTEGRATION.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) == "WebhookSecurity":
                built += 1
                host = (
                    node.args[3]
                    if len(node.args) >= 4
                    else next((k.value for k in node.keywords if k.arg == "host"), None)
                )
                assert host is not None, (
                    f"{path.name}:{node.lineno} builds WebhookSecurity without the host of the entry"
                )
                assert "host" in ast.unparse(host).lower(), (
                    f"{path.name}:{node.lineno} passes {ast.unparse(host)}, no host"
                )
    assert built, "no WebhookSecurity(...) found: the check no longer looks where the entry builds it"
