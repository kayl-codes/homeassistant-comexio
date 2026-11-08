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
a local name assigned from such an expression (tuple targets and annotated assignments included; a logging method
bound to a local name, also in a tuple or a conditional expression, counts as a sink). A nested function starts with
what its enclosing function knew at its definition. An assignment inside a branch or loop (if, for, while, try,
match) can add a taint but never clears one, since it may not run.
`redact_webhook_ids` only replaces an id that directly follows `/api/webhook/` in the text. So an id passed to it
counts as safe only as the part right after that literal in an f-string or `+` concatenation, or inside a local that
holds a whole webhook path (`hook = f"/api/webhook/{webhook_id}"`); a bare id, an id after other text, and its
keyword arguments are still checked.

Not followed: an id handed through an attribute or another function, a module-level or enclosing-scope name assigned
after the function's definition, a taint assigned later in a loop body than the use it reaches on the next pass,
augmented assignments, walrus and loop targets, and the texts the aiocomexio library
puts into its own exceptions (they quote Comexio's answer, not our request). Those stay for the review of the diff.

A second check keeps the Comexio host bound to the ledger: `WebhookSecurity(...)` must be given the host of the entry,
otherwise a changed host would keep the old id off although another server may still hold commands on it.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

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
ENTRY_ID_KEYWORD = "entry_id"

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


def _string_parts(node: ast.AST) -> list[ast.expr] | None:
    """The pieces of an f-string or a `+` concatenation in text order; None for any other expression."""
    if isinstance(node, ast.JoinedStr):
        return [part.value if isinstance(part, ast.FormattedValue) else part for part in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*(_string_parts(node.left) or [node.left]), *(_string_parts(node.right) or [node.right])]
    return None


def _survives_redaction(node: ast.AST, tainted: frozenset[str], paths: frozenset[str]) -> bool:
    """What redact_webhook_ids leaves: an id that does not directly follow a webhook path in the text."""
    if isinstance(node, ast.Name) and node.id in paths:
        return False  # a local holding a whole webhook path: the redactor replaces its id
    parts = _string_parts(node)
    if parts is None:
        return _carries_id(node, tainted, paths)
    for previous, part in zip([None, *parts[:-1]], parts, strict=True):
        after_path = isinstance(previous, ast.Constant) and str(previous.value).endswith(WEBHOOK_PATH)
        if isinstance(part, ast.Constant) or (after_path and _is_bare_secret(part, tainted)):
            continue
        if _survives_redaction(part, tainted, paths):
            return True
    return False


def _carries_id(node: ast.AST, tainted: frozenset[str] = frozenset(), paths: frozenset[str] = frozenset()) -> bool:
    """Whether an expression names the secret, reads a command's path field or a tainted name, outside redaction."""
    if isinstance(node, ast.Call) and _call_name(node) in SAFE_WRAPPERS:
        arguments = (*node.args, *(keyword.value for keyword in node.keywords))
        return any(_survives_redaction(arg, tainted, paths) for arg in arguments)
    if _is_bare_secret(node, tainted) and not isinstance(node, ast.Call):
        return True
    if isinstance(node, ast.Constant) and node.value == FORM_PATH_FIELD:
        return True
    return any(_carries_id(child, tainted, paths) for child in ast.iter_child_nodes(node))


def _binds_logging_method(value: ast.AST) -> bool:
    """A logging method taken, not called: `log = _LOGGER.warning`, also inside a tuple or a conditional."""
    called = {id(node.func) for node in ast.walk(value) if isinstance(node, ast.Call)}
    return any(
        isinstance(node, ast.Attribute) and node.attr in LOG_METHODS and id(node) not in called
        for node in ast.walk(value)
    )


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


class _Frame:
    """What one function knows: tainted names, logging aliases, locals holding a whole webhook path, branch depth.

    A nested function starts with a copy of its enclosing function's state at its definition.
    """

    def __init__(self, outer: "_Frame | None" = None) -> None:
        self.tainted: set[str] = set(outer.tainted) if outer else set()
        self.aliases: set[str] = set(outer.aliases) if outer else set()
        self.paths: set[str] = set(outer.paths) if outer else set()
        self.depth = 0

    def carries_id(self, node: ast.AST) -> bool:
        return _carries_id(node, frozenset(self.tainted), frozenset(self.paths))

    def survives_redaction(self, node: ast.AST) -> bool:
        return _survives_redaction(node, frozenset(self.tainted), frozenset(self.paths))


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.frames: list[_Frame] = [_Frame()]
        self.hits: list[tuple[str, int]] = []

    def _enter(self, node: ast.AST, name: str, *, function: bool) -> None:
        self.scope.append(name)
        if function:
            self.frames.append(_Frame(self.frames[-1]))
        self.generic_visit(node)
        if function:
            self.frames.pop()
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name, function=False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name, function=True)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _branch(self, node: ast.AST) -> None:
        """An assignment in here may not run, so it never clears a taint (see _assign)."""
        self.frames[-1].depth += 1
        self.generic_visit(node)
        self.frames[-1].depth -= 1

    visit_If = visit_For = visit_AsyncFor = visit_While = _branch
    visit_Try = visit_TryStar = visit_Match = _branch

    def _hit(self, node: ast.AST) -> None:
        self.hits.append((".".join(self.scope), node.lineno))

    def _assign(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if value is None:
            return  # `name: str` alone assigns nothing
        frame = self.frames[-1]
        names = {name for target in targets for name in _target_names(target)}
        if _binds_logging_method(value):
            frame.aliases.update(names)
        carries = frame.carries_id(value)
        single = len(targets) == 1 and isinstance(targets[0], ast.Name)
        whole_path = carries and single and not frame.survives_redaction(value)
        # in a branch a path local stays one only if every path it may hold is a whole webhook path
        keep_path = whole_path and (frame.depth == 0 or names <= frame.paths)
        frame.paths.difference_update(names)
        if keep_path:
            frame.paths.update(names)
        if carries:
            frame.tainted.update(names)
        elif frame.depth == 0:
            frame.tainted.difference_update(names)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign([node.target], node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        frame = self.frames[-1]
        arguments = (*node.args, *(k.value for k in node.keywords))
        if _is_printing(node, frozenset(frame.aliases)) and any(frame.carries_id(arg) for arg in arguments):
            self._hit(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if any(part is not None and self.frames[-1].carries_id(part) for part in (node.exc, node.cause)):
            self._hit(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if node.msg is not None and self.frames[-1].carries_id(node.msg):
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
        _LOGGER.warning("id %s", webhook_id)  # leak
        self._logger.debug("secret %s", self.secret)  # leak
        logging.getLogger(__name__).info("id %s", webhook_id)  # leak
        persistent_notification.async_create(hass, f"path {cmd['Parameter']}")  # leak
        async_create_entry_issue(hass, entry, "x", placeholders={"p": derive_webhook_id(secret, "k")})  # leak
        security.warn_refused("k", "refused %s", path_for(cls, item))  # leak
        url = f"/api/webhook/{webhook_id}"
        _LOGGER.error("url %s", url)  # leak
        raise ValueError(f"bad {webhook_id}")  # leak
        log = _LOGGER.warning
        log("alias %s", webhook_id)  # leak
        ann: str = webhook_id
        _LOGGER.info("ann %s", ann)  # leak
        first, second = cmd["Parameter"], 1
        _LOGGER.info("tuple %s", first)  # leak
        raise ValueError("x") from OSError(webhook_id)  # leak
        assert ok, f"bad {webhook_id}"  # leak
        _LOGGER.info(redact_webhook_ids(webhook_id))  # leak
        _LOGGER.info(redact_webhook_ids(f"id {webhook_id}"))  # leak
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id} {secret}"))  # leak
        _LOGGER.info(redact_webhook_ids("id " + webhook_id))  # leak
        _LOGGER.info(redact_webhook_ids(text=webhook_id))  # leak
        again = webhook_id
        if flag:
            again = "safe in one branch only"
        _LOGGER.info("again %s", again)  # leak
        log2, n = _LOGGER.warning, 1
        log2("alias %s", webhook_id)  # leak
        log3 = _LOGGER.warning if ok else _LOGGER.info
        log3("alias %s", webhook_id)  # leak
        hook = f"/api/webhook/{webhook_id}"
        if flag:
            hook = webhook_id
        _LOGGER.info(redact_webhook_ids(hook))  # leak
        token = webhook_id

        def inner():
            _LOGGER.info("token %s", token)  # leak

    async def async_leaks(self, webhook_id):
        print(webhook_id)  # leak

    def safe(self, text, cmd, webhook_id):
        persistent_notification.async_create(hass, redact_webhook_ids(text))
        _LOGGER.warning("name %s", cmd.get("Name"))
        api.register(webhook_id)
        url = f"/api/other/{cmd}"
        _LOGGER.info("url %s", url)
        raise ValueError("bad value")
        _LOGGER.info("p %s", redact_webhook_ids(f"/api/webhook/{webhook_id}"))
        _LOGGER.info(redact_webhook_ids(f"see /api/webhook/{webhook_id}"))
        hook = f"/api/webhook/{webhook_id}"
        _LOGGER.info(redact_webhook_ids(hook))
        _LOGGER.info(redact_webhook_ids(f"posting to {hook}"))
        again = webhook_id
        again = "safe now"
        _LOGGER.info("again %s", again)

        def inner_safe():
            _LOGGER.info(redact_webhook_ids(hook))
"""
    hits = _scan(source)
    marked = [number for number, line in enumerate(source.splitlines(), 1) if line.endswith("# leak")]

    assert Counter(scope for scope, _line in hits) == {"C.leaks": 22, "C.leaks.inner": 1, "C.async_leaks": 1}
    assert sorted(line for _scope, line in hits) == marked  # each marked line once, nothing else


def _argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((k.value for k in node.keywords if k.arg == keyword), None)


def _entry_host_read(host: ast.expr) -> str | None:
    """The entry whose host option `host` reads: `<entry>.data[CONF_HOST]` or `.data.get(CONF_HOST, ...)`, in str()."""
    if isinstance(host, ast.Call) and _call_name(host) == "str" and len(host.args) == 1:
        host = host.args[0]
    if isinstance(host, ast.Subscript):
        data, key = host.value, host.slice
    elif isinstance(host, ast.Call) and isinstance(host.func, ast.Attribute) and host.func.attr == "get" and host.args:
        data, key = host.func.value, host.args[0]
    else:
        return None
    if isinstance(key, ast.Name) and key.id == HOST_SOURCE and isinstance(data, ast.Attribute) and data.attr == "data":
        return ast.unparse(data.value)
    return None


def _host_problem(node: ast.Call) -> str | None:
    """Why a WebhookSecurity(...) call does not bind the host of the entry it is built for; None when it does."""
    entry_id = _argument(node, 1, ENTRY_ID_KEYWORD)
    if not (isinstance(entry_id, ast.Attribute) and entry_id.attr == ENTRY_ID_KEYWORD):
        return "no <entry>.entry_id to take the host from"
    host = _argument(node, 3, HOST_KEYWORD)
    if host is None:
        return "no host"
    if _entry_host_read(host) != ast.unparse(entry_id.value):
        return f"host {ast.unparse(host)} is not the {HOST_SOURCE} option of {ast.unparse(entry_id.value)}"
    return None


@pytest.mark.parametrize(
    ("call", "bound"),
    [
        ('WebhookSecurity(hass, entry.entry_id, sid, str(entry.data.get(CONF_HOST, "")))', True),
        ("WebhookSecurity(hass, entry.entry_id, sid, host=entry.data[CONF_HOST])", True),
        ("WebhookSecurity(hass, entry_id=entry.entry_id, server_id=sid, host=entry.data[CONF_HOST])", True),
        ("WebhookSecurity(hass, self.entry.entry_id, sid, self.entry.data[CONF_HOST])", True),
        ("WebhookSecurity(hass, self.entry.entry_id, sid, entry.data[CONF_HOST])", False),
        ("WebhookSecurity(hass, entry.entry_id, sid)", False),
        ('WebhookSecurity(hass, entry.entry_id, sid, str(other.data.get(CONF_HOST, "")))', False),
        ('WebhookSecurity(hass, entry.entry_id, sid, f"{entry.data[CONF_HOST]}x")', False),
        ("WebhookSecurity(hass, entry.entry_id, sid, CONF_HOST)", False),
        ("WebhookSecurity(hass, entry.entry_id, sid, entry.options[CONF_HOST])", False),
        ("WebhookSecurity(hass, entry_id, sid, entry.data[CONF_HOST])", False),
    ],
)
def test_the_host_check_accepts_only_the_host_option_of_the_same_entry(call: str, bound: bool) -> None:
    node = ast.parse(call, mode="eval").body
    assert isinstance(node, ast.Call)

    assert (_host_problem(node) is None) is bound


def test_every_webhook_security_is_built_with_the_host_of_its_entry() -> None:
    problems: list[str] = []
    built = 0
    for path in sorted(INTEGRATION.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) == "WebhookSecurity":
                built += 1
                if problem := _host_problem(node):
                    problems.append(f"{path.name}:{node.lineno}: {problem}")
    assert built, "no WebhookSecurity(...) found: the check no longer looks where the entry builds it"
    assert not problems, f"WebhookSecurity must be given the host of its own entry: {problems}"
