"""The webhook ids and the entry secret never reach a log line, a notification or a repair, and the host is bound.

HA's webhook endpoint takes no authentication, so the id in /api/webhook/<id> is the secret of its command (dg): whoever
reads one can forge that command's value. Anything the integration prints - a log line, a persistent notification, the
placeholders of a repair, the text of an exception HA shows and logs - can be read by other HA users or ends up in a
pasted log. The behaviour tests pin the places that were wrong once (a refused id, the preview notification, a path
deviation); this scan makes a new place a finding before a bot or a user has to find it.

What counts as printing: a call of a logging method (debug, info, warning, ... on any receiver), `print`,
`warn_refused`, `persistent_notification.async_create`, the repair creators `async_create_issue` /
`async_create_entry_issue` (their placeholders), and the text of a `raise` (also its cause) or an `assert`.
What counts as carrying an id: a name or attribute in SECRET_NAMES, the form field `Parameter` (a command's path), or
a local name assigned from such an expression in the same function (tuple targets and annotated assignments included;
a logging method bound to a local name counts as a sink). A bare id or secret handed to `redact_webhook_ids` stays
tainted: it only redacts ids inside a webhook path. `redact_webhook_ids(...)` makes its argument safe, so a use under
it is fine.

Not followed: an id handed through an attribute or another function, a logging alias or tainted name from an outer
scope (module level, enclosing function), reassignments in a branch or loop, augmented assignments, walrus and loop
targets, and the texts the aiocomexio library puts into its
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
WEBHOOK_PATH = "/api/webhook/"
HOST_KEYWORD = "host"
HOST_SOURCE = "CONF_HOST"  # the entry's own host option

# (file relative to the integration, Class.method) -> (uses counted there, why the id is not shown there). Each use
# counts, so a second one in a listed function fails too.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {}


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_printing(node: ast.Call, aliases: frozenset[str] = frozenset()) -> bool:
    name = _call_name(node)
    if name in PRINTING_CALLS:
        return True
    if isinstance(node.func, ast.Name):
        return name in aliases
    return isinstance(node.func, ast.Attribute) and name in LOG_METHODS


def _is_bare_secret(node: ast.AST, tainted: frozenset[str]) -> bool:
    """A value that is an id or the secret itself: redact_webhook_ids only knows ids inside a webhook path."""
    if isinstance(node, ast.Name):
        return node.id in SECRET_NAMES or node.id in tainted
    if isinstance(node, ast.Attribute):
        return node.attr in SECRET_NAMES
    return isinstance(node, ast.Call) and _call_name(node) in SECRET_NAMES


def _survives_redaction(node: ast.AST, tainted: frozenset[str]) -> bool:
    """What redact_webhook_ids leaves: a bare id, or an id in a text that is not shaped like a webhook path."""
    if _is_bare_secret(node, tainted):
        return True
    if isinstance(node, ast.JoinedStr):
        in_path = any(isinstance(p, ast.Constant) and WEBHOOK_PATH in str(p.value) for p in node.values)
        return not in_path and _carries_id(node, tainted)
    return False


def _carries_id(node: ast.AST, tainted: frozenset[str] = frozenset()) -> bool:
    """Whether an expression names the secret, reads a command's path field or a tainted name, outside redaction."""
    if isinstance(node, ast.Call) and _call_name(node) in SAFE_WRAPPERS:
        return any(_survives_redaction(arg, tainted) for arg in node.args)
    if _is_bare_secret(node, tainted) and not isinstance(node, ast.Call):
        return True
    if isinstance(node, ast.Constant) and node.value == FORM_PATH_FIELD:
        return True
    return any(_carries_id(child, tainted) for child in ast.iter_child_nodes(node))


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.tainted: list[set[str]] = [set()]
        self.aliases: list[set[str]] = [set()]
        self.hits: list[tuple[str, int]] = []

    def _enter(self, node: ast.AST, name: str, *, function: bool) -> None:
        self.scope.append(name)
        if function:
            self.tainted.append(set())
            self.aliases.append(set())
        self.generic_visit(node)
        if function:
            self.tainted.pop()
            self.aliases.pop()
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name, function=False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name, function=True)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _hit(self, node: ast.AST) -> None:
        self.hits.append((".".join(self.scope), node.lineno))

    def _assign(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        names = [name for target in targets for name in _target_names(target)]
        if isinstance(value, ast.Attribute) and value.attr in LOG_METHODS:
            self.aliases[-1].update(names)
        if value is not None and _carries_id(value, frozenset(self.tainted[-1])):
            self.tainted[-1].update(names)
        else:
            self.tainted[-1].difference_update(names)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign([node.target], node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        tainted = frozenset(self.tainted[-1])
        arguments = (*node.args, *(k.value for k in node.keywords))
        if _is_printing(node, frozenset(self.aliases[-1])) and any(_carries_id(arg, tainted) for arg in arguments):
            self._hit(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        tainted = frozenset(self.tainted[-1])
        if any(part is not None and _carries_id(part, tainted) for part in (node.exc, node.cause)):
            self._hit(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if node.msg is not None and _carries_id(node.msg, frozenset(self.tainted[-1])):
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
        log = _LOGGER.warning
        log("alias %s", webhook_id)
        ann: str = webhook_id
        _LOGGER.info("ann %s", ann)
        first, second = cmd["Parameter"], 1
        _LOGGER.info("tuple %s", first)
        raise ValueError("x") from OSError(webhook_id)
        assert ok, f"bad {webhook_id}"
        _LOGGER.info(redact_webhook_ids(webhook_id))
        _LOGGER.info(redact_webhook_ids(f"id {webhook_id}"))

    async def async_leaks(self, webhook_id):
        print(webhook_id)

    def safe(self, text, cmd, webhook_id):
        persistent_notification.async_create(hass, redact_webhook_ids(text))
        _LOGGER.warning("name %s", cmd.get("Name"))
        api.register(webhook_id)
        url = f"/api/other/{cmd}"
        _LOGGER.info("url %s", url)
        raise ValueError("bad value")
        _LOGGER.info("p %s", redact_webhook_ids(f"/api/webhook/{webhook_id}"))
        again = webhook_id
        again = "safe now"
        _LOGGER.info("again %s", again)
"""
    scopes = Counter(scope for scope, _line in _scan(source))

    assert scopes == {"C.leaks": 15, "C.async_leaks": 1}


def test_every_webhook_security_is_built_with_the_host_of_its_entry() -> None:
    built = 0
    for path in sorted(INTEGRATION.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) == "WebhookSecurity":
                built += 1
                host = node.args[3] if len(node.args) >= 4 else None
                host = host or next((k.value for k in node.keywords if k.arg == HOST_KEYWORD), None)
                assert host is not None, f"{path.name}:{node.lineno} builds WebhookSecurity without the host"
                assert HOST_SOURCE in ast.unparse(host), f"{path.name}:{node.lineno} passes {ast.unparse(host)}"
    assert built, "no WebhookSecurity(...) found: the check no longer looks where the entry builds it"
