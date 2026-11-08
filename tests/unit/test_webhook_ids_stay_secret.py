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
a local name assigned from such an expression (tuple targets and annotated assignments included; a sink - logging
method, print, notification or repair creator, also via `functools.partial` - bound to a local name, also in a tuple
or a conditional expression, still counts as one). A nested function starts with
what its enclosing function knew at its definition. An assignment inside a branch or loop (if, for, while, try,
match) can add a taint but never clears one, since it may not run.
A parameter default, a walrus target, an augmented assignment, a loop, comprehension or `with` target fed from an id
and a `case {"Parameter": name}` capture carry like an assignment; a class body has a scope of its own that its
methods do not see, a lambda has one too, a name assigned the string `Parameter` in a scope (or one it inherits)
reads as that field there, and `from logging import warning` (or any import of a sink) makes `warning` a sink. A
`partial` that binds an id into a sink is a finding where it is made.
The flow through the module is followed conservatively, by name, until it stops growing: an id stored in an attribute
(`holder.value = webhook_id`, `self._ids[k] = ...`) taints that attribute name on every receiver in the file, an id
put into a container (`box.append(id)`, `table[k] = id`) taints the container, an id passed to a function or method of
the module taints the parameter it lands in (all of them behind `*`/`**`; a constructor call reaches `__init__`, or
the annotated fields of a class without one; a function of the module handed over as a positional argument, as in
`call_later(1, self._log, webhook_id)` or `partial(self._log, webhook_id)`, receives the arguments behind it in order
and the keywords by name), and a function that returns or yields an id (or the secret) taints every call of its name.
`redact_webhook_ids` only replaces an id that directly follows `/api/webhook/` in the text. So an id passed to it
counts as safe only as the part right after that literal in an f-string or `+` concatenation (plain, no `!r` or
format spec: those change the text), or inside a local that holds a whole webhook path
(`hook = f"/api/webhook/{webhook_id}"`); the entry secret is never safe there, since it is no 32-hex id - not even
in a local, attribute or parameter called like an id (`webhook_id = secret`) - and a local name is not trusted to hold
an id either. A bare id, an id after other text, and its keyword arguments are still checked. Only the bare name
`redact_webhook_ids`, imported from the integration's own `webhook_security`, is the redactor.
The old id `comexio_<server_id>` is not replaced by it, and is no finding either: it is derived from the server id
that every entity_id shows, so anyone who can read the log can build it, and it is switched off after the migration.
An exception being handled counts like a named one: when the `try` body handled an id or the secret, a log call in
its handler that prints the traceback (`.exception(...)`, `exc_info=...`, `traceback.print_exc()`) is a finding.

Not followed: a value handed on through another module, `setattr`/`getattr` with a computed name, a mutating method
outside MUTATORS, a class or function reached through another name than its own (an alias, a returned callable), a
callback handed over by keyword or an argument placed before the callback, a constructor reached through an
attribute (`self.Holder(...)`), a base class named through an attribute (`pkg.Base`), a stream reached through a
local alias or a deeper attribute (`err = sys.stderr; err.write(...)`, `sys.stdout.buffer.write(...)`), a traceback
printed by a function the handler calls or by an own wrapper (`self._log(msg, exc_info=True)`), a traceback printer
reached through an import alias (`from traceback import format_exc as fe`), a module-level
import or alias
placed after the function that uses it, a module-level or enclosing-scope name assigned after the function's
definition, a taint assigned later in a loop body than the use it reaches on the next pass, and the texts the
aiocomexio library puts into its own exceptions (they quote Comexio's answer, not our request). Those stay for the
review of the diff.

A second check keeps the Comexio host bound to the ledger: `WebhookSecurity(...)` must be given the host of the entry,
otherwise a changed host would keep the old id off although another server may still hold commands on it. The class
may only be called or named in an annotation: an alias, a partial or a factory argument would hide a construction
from that check, so any other use of it is a finding too.
"""

import ast
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pytest

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"

# The only places that hold an id or the secret today: the handler's `webhook_id`, `security.secret`,
# `derive_webhook_id` (coordinator) and the path callables (`command_path`, `WebhookSecurity.path_for`).
ENTRY_SECRET = "secret"
SECRET_NAMES = frozenset({"webhook_id", "derive_webhook_id", "command_path", "path_for", ENTRY_SECRET})
ID_NAMES = SECRET_NAMES - {ENTRY_SECRET}  # what the redactor can know: a 32-hex id, never the entry secret
FORM_PATH_FIELD = "Parameter"
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"})
PRINTING_CALLS = frozenset(
    {
        "print",
        "pprint",
        "warn_refused",
        "warn_explicit",
        "showwarning",
        "add_note",  # an exception note ends up in the traceback
        "async_create",
        "async_create_issue",
        "async_create_entry_issue",
    }
)
TRACEBACK_CALLS = frozenset({"print_exc", "format_exc", "print_exception", "format_exception", "print_last"})
STREAM_NAMES = frozenset({"stdout", "stderr"})  # `sys.stderr.write(...)` prints like `print`
STREAM_WRITES = frozenset({"write", "writelines"})
SINK_NAMES = LOG_METHODS | PRINTING_CALLS
SAFE_WRAPPERS = frozenset({"redact_webhook_ids"})
# methods that put their argument into the receiver: the container then holds the id
MUTATORS = frozenset({"append", "appendleft", "extend", "extendleft", "add", "insert", "update", "setdefault", "put"})
RECEIVERS = frozenset({"self", "cls"})  # an instance is no container: its attributes are followed by name
WEBHOOK_PATH = "/api/webhook/"
HOST_KEYWORD = "host"
HOST_SOURCE = "CONF_HOST"  # the entry's own host option
PRODUCTION_MODULE = "custom_components.comexio.webhook_security"  # where redact_webhook_ids really lives
SECURITY_CLASS = "WebhookSecurity"
ENTRY_ID_KEYWORD = "entry_id"

# (file relative to the integration, Class.method) -> (uses counted there, why the id is not shown there). Each use
# counts, so a second one in a listed function fails too.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("__init__.py", "_webhook_handler.handle_webhook"): (
        1,
        "the `try` hands webhook_id to WebhookSecurity.accepts only, and HA dispatches only ids registered by "
        "sync_webhooks (ASCII hex or comexio_<server>); `_same` and derive_webhook_id can raise only on a payload "
        "character that is not encodable, and that error quotes the payload character, never the id",
    ),
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_printing(node: ast.Call, aliases: frozenset[str] = frozenset()) -> bool:
    name = _call_name(node)
    if name in PRINTING_CALLS:
        return True
    if isinstance(node.func, ast.Name):
        return name in aliases
    if not isinstance(node.func, ast.Attribute):
        return False
    receiver = node.func.value
    tail = (
        receiver.attr if isinstance(receiver, ast.Attribute) else receiver.id if isinstance(receiver, ast.Name) else ""
    )
    return name in LOG_METHODS or (name in STREAM_WRITES and tail in STREAM_NAMES)


def _prints_traceback(node: ast.Call, *, printing: bool) -> bool:
    """A call that puts the exception being handled into a log or onto a stream without naming it:
    `_LOGGER.exception(...)`, a log call with `exc_info=True` (or anything but a constant that is falsy), or one of the
    traceback printers."""
    if _call_name(node) in TRACEBACK_CALLS:
        return True
    wants_it = any(  # `**options` may hold exc_info: no way to tell, so it counts
        keyword.arg is None
        or (keyword.arg == "exc_info" and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value))
        for keyword in node.keywords
    )
    return printing and (_call_name(node) == "exception" or wants_it)


@dataclass(frozen=True)
class _Taint:
    """What an expression is checked against: a function's tainted locals and whole-path locals, the locals that may
    hold the entry secret, and what the module-wide flow learned (see _Flow)."""

    names: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()
    attrs: frozenset[str] = frozenset()
    secret_attrs: frozenset[str] = frozenset()
    calls: frozenset[str] = frozenset()
    secret_calls: frozenset[str] = frozenset()
    shadowed: bool = False  # the module binds the redactor's name to something else: no call of it is trusted


_NO_TAINT = _Taint()


def _is_bare_secret(node: ast.AST, taint: _Taint, *, ids_only: bool = False) -> bool:
    """A value that is an id or the secret itself: redact_webhook_ids only knows ids inside a webhook path."""
    if ids_only:  # trusted as an id: a name called like one that cannot hold the entry secret
        if isinstance(node, ast.Name):
            return node.id in ID_NAMES and node.id not in taint.secrets
        if isinstance(node, ast.Attribute):
            return node.attr in ID_NAMES and node.attr not in taint.secret_attrs
        return (
            isinstance(node, ast.Call) and _call_name(node) in ID_NAMES and _call_name(node) not in taint.secret_calls
        )
    if isinstance(node, ast.Name):
        return node.id in SECRET_NAMES or node.id in taint.names  # a local may hold the entry secret
    if isinstance(node, ast.Attribute):
        return node.attr in SECRET_NAMES or node.attr in taint.attrs
    return isinstance(node, ast.Call) and (_call_name(node) in SECRET_NAMES or _call_name(node) in taint.calls)


def _holds_secret(node: ast.AST, taint: _Taint) -> bool:
    """Whether a value may be the entry secret itself: it names the secret outside a call that derives an id from it."""
    if isinstance(node, ast.Call) and _call_name(node) in taint.secret_calls:
        return True  # the module's own function of that name returns the secret, whatever it is called
    if isinstance(node, ast.Call) and _call_name(node) in ID_NAMES:
        return False  # trusted by name, like a parameter called like an id
    if isinstance(node, ast.Name) and (node.id == ENTRY_SECRET or node.id in taint.secrets):
        return True
    if isinstance(node, ast.Attribute) and (node.attr == ENTRY_SECRET or node.attr in taint.secret_attrs):
        return True
    return any(_holds_secret(child, taint) for child in ast.iter_child_nodes(node))


def _plain_value(part: ast.expr) -> ast.expr:
    """The value of an f-string field; a field with `!r` or a format spec renders other text, so it stays whole."""
    if isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
        return part.value
    return part


def _string_parts(node: ast.AST) -> list[ast.expr] | None:
    """The pieces of an f-string or a `+` concatenation in text order; None for any other expression."""
    if isinstance(node, ast.JoinedStr):
        return [_plain_value(part) for part in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*(_string_parts(node.left) or [node.left]), *(_string_parts(node.right) or [node.right])]
    return None


def _survives_redaction(node: ast.AST, taint: _Taint) -> bool:
    """What redact_webhook_ids leaves: an id that does not directly follow a webhook path in the text."""
    if isinstance(node, ast.Name) and node.id in taint.paths:
        return False  # a local holding a whole webhook path: the redactor replaces its id
    parts = _string_parts(node)
    if parts is None:
        return _carries_id(node, taint)
    for previous, part in zip([None, *parts[:-1]], parts, strict=True):
        after_path = isinstance(previous, ast.Constant) and str(previous.value).endswith(WEBHOOK_PATH)
        if isinstance(part, ast.Constant) or (after_path and _is_bare_secret(part, taint, ids_only=True)):
            continue
        if _survives_redaction(part, taint):
            return True
    return False


def _carries_id(node: ast.AST, taint: _Taint = _NO_TAINT) -> bool:
    """Whether an expression names the secret, reads a command's path field or a tainted name, outside redaction."""
    # only the bare name the integration imports: `other.redact_webhook_ids(...)` is some other function
    redaction = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SAFE_WRAPPERS
    if isinstance(node, ast.Call) and redaction and not taint.shadowed:
        arguments = (*node.args, *(keyword.value for keyword in node.keywords))
        return any(_survives_redaction(arg, taint) for arg in arguments)
    if _is_bare_secret(node, taint):
        return True
    if isinstance(node, ast.Constant) and node.value == FORM_PATH_FIELD:
        return True
    return any(_carries_id(child, taint) for child in ast.iter_child_nodes(node))


def _binds_sink(value: ast.AST, aliases: frozenset[str] = frozenset()) -> bool:
    """A sink taken, not called: `log = _LOGGER.warning`, `notify = persistent_notification.async_create`, a copy of
    such a name, also inside a tuple, a conditional or `functools.partial(...)`."""
    called = {id(node.func) for node in ast.walk(value) if isinstance(node, ast.Call)}
    return any(
        id(node) not in called
        and (
            (isinstance(node, ast.Attribute) and node.attr in SINK_NAMES)
            or (isinstance(node, ast.Name) and (node.id in PRINTING_CALLS or node.id in aliases))
        )
        for node in ast.walk(value)
    )


def _tainted_defaults(node: ast.AST, outer: "_Frame", *, secret: bool = False) -> set[str]:
    """The parameters whose default value carries an id or a command's path (`secret`: may be the secret itself)."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    check = outer.holds_secret if secret else outer.carries_id
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    pairs = [
        *zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True),
        *zip(args.kwonlyargs, args.kw_defaults, strict=True),
    ]
    return {arg.arg for arg, default in pairs if default is not None and check(default)}


def _mapping_captures(case: ast.match_case) -> set[str]:
    """The names a `case {"Parameter": name}` pattern binds to a command's path."""
    names: set[str] = set()
    for pattern in ast.walk(case.pattern):
        mapping = pattern.pattern if isinstance(pattern, ast.MatchAs) else pattern
        if not isinstance(mapping, ast.MatchMapping):
            continue
        for key, sub in zip(mapping.keys, mapping.patterns, strict=True):
            if isinstance(key, ast.Constant) and key.value == FORM_PATH_FIELD:
                names |= {p.name for p in ast.walk(sub) if isinstance(p, (ast.MatchAs, ast.MatchStar)) and p.name}
                if isinstance(pattern, ast.MatchAs) and pattern.name:
                    names.add(pattern.name)  # `case {"Parameter": p} as whole`: whole holds the path too
    return names


@dataclass
class _Flow:
    """What the passes over a module learned, by name: attributes an id (or the secret) is stored in, functions that
    return or yield an id, and parameters an id (or the secret) is passed to, as (function, parameter)."""

    attrs: set[str] = field(default_factory=set)
    secret_attrs: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    secret_calls: set[str] = field(default_factory=set)
    params: set[tuple[str, str]] = field(default_factory=set)
    secret_params: set[tuple[str, str]] = field(default_factory=set)
    shadowed: bool = False  # set once for the module, not learned: the redactor's name is bound to something else

    def absorb(self, other: "_Flow") -> bool:
        """Adds what `other` learned; whether that was anything new."""
        grew = False
        for name in ("attrs", "secret_attrs", "calls", "secret_calls", "params", "secret_params"):
            mine, theirs = getattr(self, name), getattr(other, name)
            grew = grew or not theirs <= mine
            mine |= theirs
        return grew


class _Function(NamedTuple):
    """A function of the module as a call reaches it."""

    args: ast.arguments
    skipped: int  # leading parameters a call does not fill: self or cls of a method, self of a constructor
    method: bool  # reached through an attribute (a method) or through a bare name (a function, a constructor)
    # What its parameters are learned under: the def's name, `<Class>.__init__` for a constructor call. A call of a
    # method named `__init__` other than `super().__init__(...)` cannot tell the class, so it reaches every `__init__`.
    owner: str


@dataclass
class _Module:
    """The functions of a module by the name a call uses, its classes, the annotated fields of each class, the names
    of its base classes and the classes that define `__init__` themselves."""

    functions: dict[str, list[_Function]] = field(default_factory=dict)
    classes: set[str] = field(default_factory=set)
    fields: dict[str, set[str]] = field(default_factory=dict)
    bases: dict[str, list[str]] = field(default_factory=dict)
    inits: set[str] = field(default_factory=set)

    def add(self, node: ast.FunctionDef | ast.AsyncFunctionDef, cls: ast.ClassDef | None) -> None:
        static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
        method = cls is not None
        self.functions.setdefault(node.name, []).append(
            _Function(node.args, int(method and not static), method, node.name)
        )
        if cls is not None and node.name == "__init__":  # `Holder(webhook_id)` lands in `__init__`
            self.functions.setdefault(cls.name, []).append(_Function(node.args, 1, False, f"{cls.name}.__init__"))
            self.inits.add(cls.name)

    def inherit_constructors(self) -> None:
        """A class without `__init__` (`class Child(Holder): pass`) passes its arguments to the `__init__` of every
        ancestor that has one - not only the one its MRO picks - and keeps the annotated fields of every ancestor, as
        a dataclass's generated `__init__` does. Over-approximated on purpose: whether a dataclass generates its
        `__init__` (`init=False`) or which line of bases the MRO picks is not decided."""
        own = frozenset(self.inits)
        for name in self.classes - own:
            ancestors = self.ancestors(name)
            self.fields[name] |= {item for base in ancestors for item in self.fields.get(base, ())}
            for base in ancestors & own:
                ctor = f"{base}.__init__"
                self.functions.setdefault(name, []).extend(f for f in self.functions[base] if f.owner == ctor)

    def ancestors(self, name: str) -> set[str]:
        """Every base class of the module above `name`."""
        seen: set[str] = set()
        todo = list(self.bases.get(name, ()))
        while todo:
            base = todo.pop()
            if base not in seen and base != name:
                seen.add(base)
                todo += self.bases.get(base, ())
        return seen


def _is_super_init(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "__init__" and _call_name_is(func.value, "super")


def _call_name_is(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _module(tree: ast.AST) -> _Module:
    module = _Module()

    def walk(node: ast.AST, cls: ast.ClassDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                module.classes.add(child.name)
                module.bases[child.name] = [base.id for base in child.bases if isinstance(base, ast.Name)]
                module.fields[child.name] = {
                    item.target.id
                    for item in child.body
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                }
                walk(child, child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                module.add(child, cls)
                walk(child, None)  # a function inside a method is a function
            else:
                walk(child, cls)  # a method may sit in an `if` or `try` of the class body

    walk(tree, None)
    module.inherit_constructors()
    return module


def _parameters(args: ast.arguments, skipped: int) -> tuple[list[str], str, str, set[str]]:
    """The positional parameters a call fills, the `*` and `**` names ("" if none), and every parameter of them."""
    positional = [arg.arg for arg in (*args.posonlyargs, *args.args)][skipped:]
    vararg, kwarg = (args.vararg.arg if args.vararg else ""), (args.kwarg.arg if args.kwarg else "")
    everything = {*positional, *(arg.arg for arg in args.kwonlyargs), vararg, kwarg} - {""}
    return positional, vararg, kwarg, everything


def _receiving_parameters(
    call: ast.Call, args: ast.arguments, skipped: int, carries: Callable[[ast.AST], bool]
) -> set[str]:
    """The parameters an argument of `call` for which `carries` holds lands in (every one behind `*` or `**`)."""
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    if not any(carries(value) for value in values):
        return set()
    positional, vararg, kwarg, everything = _parameters(args, skipped)
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(k.arg is None for k in call.keywords):
        return everything  # unpacked: any parameter may receive it
    named = {*positional, *(arg.arg for arg in args.kwonlyargs)}  # what a keyword can name; the rest goes to **
    received = {positional[i] if i < len(positional) else vararg for i, arg in enumerate(call.args) if carries(arg)}
    received |= {k.arg if k.arg in named else kwarg for k in call.keywords if carries(k.value)}
    return received - {""}


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

    def __init__(self, outer: "_Frame | None" = None, *, is_class: bool = False, flow: _Flow | None = None) -> None:
        self.parent = outer
        self.is_class = is_class
        self.flow = outer.flow if outer else flow or _Flow()
        self.tainted: set[str] = set(outer.tainted) if outer else set()
        self.secrets: set[str] = set(outer.secrets) if outer else set()
        self.aliases: set[str] = set(outer.aliases) if outer else set()
        self.paths: set[str] = set(outer.paths) if outer else set()
        self.depth = 0
        self.handling = (
            False  # inside an `except` whose `try` body handled an id or the secret: its traceback may quote it
        )

    def taint(self) -> _Taint:
        flow = self.flow
        return _Taint(
            frozenset(self.tainted),
            frozenset(self.paths),
            frozenset(self.secrets),
            frozenset(flow.attrs),
            frozenset(flow.secret_attrs),
            frozenset(flow.calls),
            frozenset(flow.secret_calls),
            flow.shadowed,
        )

    def carries_id(self, node: ast.AST) -> bool:
        return _carries_id(node, self.taint())

    def holds_secret(self, node: ast.AST) -> bool:
        return _holds_secret(node, self.taint())

    def survives_redaction(self, node: ast.AST) -> bool:
        return _survives_redaction(node, self.taint())


class _Scan(ast.NodeVisitor):
    """One pass over a module: reads what earlier passes learned (`flow`), records what this one learns (`learned`)."""

    def __init__(self, flow: _Flow | None = None, module: _Module | None = None) -> None:
        self.scope: list[str] = []
        self.owners: list[str | None] = []  # the function a `return` belongs to
        self.frames: list[_Frame] = [_Frame(flow=flow)]
        self.module = module or _Module()
        self.learned = _Flow()
        self.hits: list[tuple[str, int]] = []

    def _enter(self, node: ast.AST, name: str, *, function: bool, is_class: bool = False) -> None:
        self.scope.append(name)
        self.owners.append(None if is_class or isinstance(node, ast.Lambda) else name)
        if function:
            outer = self.frames[-1]
            lexical = outer  # a class body is no enclosing scope: its methods read the function around the class
            while lexical.is_class and lexical.parent:
                lexical = lexical.parent
            frame = _Frame(lexical, is_class=is_class)
            self.frames.append(frame)
            own = (
                {arg.arg for arg in ast.walk(node.args) if isinstance(arg, ast.arg)} if hasattr(node, "args") else set()
            )
            for names in (frame.tainted, frame.secrets, frame.paths, frame.aliases):
                names.difference_update(own)  # a parameter hides the outer name it shares
            frame.tainted |= _tainted_defaults(node, outer)  # defaults are read where the def stands
            frame.secrets |= _tainted_defaults(node, outer, secret=True)
            owners = {name, ".".join(self.scope[-2:])}  # `Holder.__init__` holds what `Holder(...)` passes
            frame.tainted |= {param for owner, param in frame.flow.params if owner in owners}
            frame.secrets |= {param for owner, param in frame.flow.secret_params if owner in owners}
        self.generic_visit(node)
        if function:
            self.frames.pop()
        self.owners.pop()
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name, function=True, is_class=True)  # its names are not the function's

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._enter(node, "<lambda>", function=True)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name, function=True)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _branch(self, node: ast.AST) -> None:
        """An assignment in here may not run, so it never clears a taint (see _assign)."""
        self.frames[-1].depth += 1
        self.generic_visit(node)
        self.frames[-1].depth -= 1

    visit_If = visit_While = visit_IfExp = visit_BoolOp = _branch

    def _try(self, node: ast.Try | ast.TryStar) -> None:
        """What a `try` body handles may quote it in the exception (a failed request names its URL): the handler's
        `except ... as err` then holds the id or the secret, whatever the body made of it."""
        frame = self.frames[-1]
        frame.depth += 1
        for statement in node.body:
            self.visit(statement)
        ids = any(frame.carries_id(statement) for statement in node.body)
        secret = any(frame.holds_secret(statement) for statement in node.body)
        for handler in node.handlers:
            if handler.name and ids:
                frame.tainted.add(handler.name)
            if handler.name and secret:
                frame.secrets.add(handler.name)
            outside, frame.handling = frame.handling, frame.handling or ids or secret
            self.visit(handler)
            frame.handling = outside
        for statement in node.orelse:
            self.visit(statement)
        outside, frame.handling = frame.handling, frame.handling or ids or secret  # a propagating one reaches `finally`
        for statement in node.finalbody:
            self.visit(statement)
        frame.handling = outside
        frame.depth -= 1

    visit_Try = visit_TryStar = _try

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self._store_from(node.target, node.iter)
        self._branch(node)

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.iter)
        self._store_from(node.target, node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """The loops first: their targets are bound before the element is built."""
        for generator in node.generators:
            self.visit(generator)
        for part in (getattr(node, "elt", None), getattr(node, "key", None), getattr(node, "value", None)):
            if part is not None:
                self.visit(part)

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comprehension

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._store_from(item.optional_vars, item.context_expr)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._store_from(node.target, node.value)
        if isinstance(node.target, ast.Name) and self.frames[-1].carries_id(node.value):
            self.frames[-1].paths.discard(node.target.id)  # `hook += webhook_id`: no longer only a whole path
        self.generic_visit(node)

    def _learn_return(self, node: ast.Return | ast.Yield | ast.YieldFrom) -> None:
        owner = self.owners[-1] if self.owners else None
        if owner and node.value is not None:
            if self.frames[-1].carries_id(node.value):
                self.learned.calls.add(owner)
            if self.frames[-1].holds_secret(node.value):
                self.learned.secret_calls.add(owner)
        self.generic_visit(node)

    visit_Return = visit_Yield = visit_YieldFrom = _learn_return

    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            self.frames[-1].tainted |= _mapping_captures(case)
        self._branch(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        sinks = SINK_NAMES if node.module == "logging" else PRINTING_CALLS  # `info` of another module is no logger
        self.frames[-1].aliases.update(a.asname or a.name for a in node.names if a.name in sinks)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._assign([node.target], node.value)
        self.generic_visit(node)

    def _hit(self, node: ast.AST) -> None:
        self.hits.append((".".join(self.scope), node.lineno))

    def _store(self, target: ast.AST, *, secret: bool) -> None:
        """Taints where a value carrying an id goes: a local, an attribute name, the container of an item."""
        if isinstance(target, ast.Name) and target.id not in RECEIVERS:
            frame = self.frames[-1]
            (frame.secrets if secret else frame.tainted).add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._store(element, secret=secret)
        elif isinstance(target, (ast.Starred, ast.Subscript)):
            self._store(target.value, secret=secret)  # `table[k] = id`: the table holds it
        elif isinstance(target, ast.Attribute):
            (self.learned.secret_attrs if secret else self.learned.attrs).add(target.attr)

    def _store_from(self, target: ast.AST, value: ast.AST) -> None:
        """A binding that can only add a taint: an augmented assignment, a loop, comprehension or `with` target."""
        frame = self.frames[-1]
        if frame.carries_id(value):
            self._store(target, secret=False)
        if frame.holds_secret(value):
            self._store(target, secret=True)

    def _assign(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if value is None:
            return  # `name: str` alone assigns nothing
        frame = self.frames[-1]
        names = {name for target in targets for name in _target_names(target)}
        if _binds_sink(value, frozenset(frame.aliases)):
            frame.aliases.update(names)
        carries = frame.carries_id(value)
        single = len(targets) == 1 and isinstance(targets[0], ast.Name)
        whole_path = carries and single and not frame.survives_redaction(value)
        # in a branch a path local stays one only if every path it may hold is a whole webhook path
        keep_path = whole_path and (frame.depth == 0 or names <= frame.paths)
        frame.paths.difference_update(names)
        if keep_path:
            frame.paths.update(names)
        holds_secret = frame.holds_secret(value)
        if frame.depth == 0:
            frame.tainted.difference_update(names)
            frame.secrets.difference_update(names)
        for target in targets:
            if carries:
                self._store(target, secret=False)
            if holds_secret:
                self._store(target, secret=True)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign([node.target], node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        frame = self.frames[-1]
        arguments = (*node.args, *(k.value for k in node.keywords))
        printing = _is_printing(node, frozenset(frame.aliases))
        partial = (
            _call_name(node) == "partial" and bool(node.args) and _binds_sink(node.args[0], frozenset(frame.aliases))
        )
        carrying = arguments[1:] if partial else arguments  # a partial's first argument is the sink itself
        leaking = (printing or partial) and any(frame.carries_id(arg) for arg in carrying)
        if leaking or (frame.handling and _prints_traceback(node, printing=printing)):
            self._hit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:  # `box.append(webhook_id)`: it holds it
            if any(frame.carries_id(arg) for arg in arguments):
                self._store(node.func.value, secret=False)
            if any(frame.holds_secret(arg) for arg in arguments):
                self._store(node.func.value, secret=True)
        self._learn_arguments(node)
        self.generic_visit(node)

    def _callees(self, node: ast.Call) -> Iterator[tuple[_Function, int]]:
        """The functions of the module a call may reach, with the parameters it skips."""
        if _is_super_init(node):  # `super().__init__(...)` reaches the constructors of the bases around it
            for base in self._ancestors_around() & self.module.inits:
                yield from ((f, 1) for f in self.module.functions[base] if f.owner == f"{base}.__init__")
            return
        by_attribute = isinstance(node.func, ast.Attribute)
        for function in self.module.functions.get(_call_name(node), ()):
            if function.method == by_attribute:  # a method through an attribute, a function through its bare name
                yield function, 0 if self._through_class(node.func) else function.skipped

    def _through_class(self, ref: ast.AST) -> bool:
        """`C._log`: a method reached through its class is given self like any argument."""
        return (
            isinstance(ref, ast.Attribute) and isinstance(ref.value, ast.Name) and ref.value.id in self.module.classes
        )

    def _learn_arguments(self, node: ast.Call) -> None:
        """Taints the parameters of a function of this module that an id or the secret is passed to."""
        frame = self.frames[-1]
        for function, skipped in self._callees(node):
            self._learn_parameters(node, function, skipped)
        name = _call_name(node)
        # without an `__init__` of its own (a dataclass, a subclass) a class keeps its arguments in the annotated fields
        # of it and its ancestors; an own `__init__` is followed through its parameters instead
        kept = self.module.fields[name] if name in self.module.fields and name not in self.module.inits else set()
        if _is_super_init(node):  # `super().__init__(webhook_id)` into a base without one, such as a dataclass
            kept = {item for base in self._ancestors_around() for item in self.module.fields.get(base, ())}
        values = (*node.args, *(k.value for k in node.keywords))
        if kept and any(frame.carries_id(value) for value in values):
            self.learned.attrs |= kept
        if kept and any(frame.holds_secret(value) for value in values):
            self.learned.secret_attrs |= kept
        self._learn_callbacks(node)

    def _ancestors_around(self) -> set[str]:
        """Every ancestor of the classes the current code sits in."""
        return {base for name in self.scope if name in self.module.classes for base in self.module.ancestors(name)}

    def _learn_callbacks(self, node: ast.Call) -> None:
        """A function of the module handed over as a positional argument (`call_later(1, self._log, webhook_id)`,
        `partial(self._log, webhook_id)`): it receives the arguments behind it in order, the keywords by name."""
        for index, ref in enumerate(node.args):
            if not isinstance(ref, (ast.Name, ast.Attribute)):
                continue
            passed = ast.Call(func=ref, args=node.args[index + 1 :], keywords=node.keywords)
            name = ref.id if isinstance(ref, ast.Name) else ref.attr
            for function in self.module.functions.get(name, ()):
                if function.method == isinstance(ref, ast.Attribute):
                    self._learn_parameters(passed, function, 0 if self._through_class(ref) else None)

    def _learn_parameters(self, call: ast.Call, function: _Function, skipped: int | None = None) -> None:
        frame, skip = self.frames[-1], function.skipped if skipped is None else skipped
        ids = _receiving_parameters(call, function.args, skip, frame.carries_id)
        secrets = _receiving_parameters(call, function.args, skip, frame.holds_secret)
        self.learned.params |= {(function.owner, param) for param in ids}
        self.learned.secret_params |= {(function.owner, param) for param in secrets}

    def visit_Raise(self, node: ast.Raise) -> None:
        if any(part is not None and self.frames[-1].carries_id(part) for part in (node.exc, node.cause)):
            self._hit(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if node.msg is not None and self.frames[-1].carries_id(node.msg):
            self._hit(node)
        self.generic_visit(node)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a scope, without the insides of the functions and classes nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _path_field_names(scope: ast.AST) -> tuple[set[str], set[str]]:
    """The names a scope assigns only the string `Parameter`, and every name it binds (which hide an outer one)."""
    fields: set[str] = set()
    field_targets: set[int] = set()
    stores: list[ast.Name] = []
    for node in _own_nodes(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(node.value, ast.Constant) and node.value.value == FORM_PATH_FIELD:
                names = [name for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)]
                fields |= {name.id for name in names}
                field_targets |= {id(name) for name in names}
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores.append(node)
    bound = {name.id for name in stores}
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        bound |= {arg.arg for arg in ast.walk(scope.args) if isinstance(arg, ast.arg)}
    bound |= {node.name for node in _own_nodes(scope) if isinstance(node, _SCOPES) and not isinstance(node, ast.Lambda)}
    other = {name.id for name in stores if id(name) not in field_targets} | (bound - {n.id for n in stores})
    return fields - other, bound


def _inline_path_field(tree: ast.AST) -> ast.AST:
    """Reads of a name a scope assigns only the string `Parameter` (`PATH_FIELD = "Parameter"`) become that literal,
    in that scope and in the scopes nested in it that do not bind the name themselves."""

    class _Inline(ast.NodeTransformer):
        def __init__(self) -> None:
            self.visible: list[tuple[set[str], bool]] = [(set(), False)]  # per scope: names read as the field, class?

        def _scope(self, node: ast.AST) -> ast.AST:
            fields, bound = _path_field_names(node)
            outer = next(names for names, is_class in reversed(self.visible) if not is_class)  # a class body hides
            self.visible.append(((outer - bound) | fields, isinstance(node, ast.ClassDef)))
            self.generic_visit(node)
            self.visible.pop()
            return node

        visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = visit_ClassDef = _scope

        def visit_Name(self, node: ast.Name) -> ast.AST:
            if isinstance(node.ctx, ast.Load) and node.id in self.visible[-1][0]:
                return ast.copy_location(ast.Constant(FORM_PATH_FIELD), node)
            return node

    return _Inline().visit(tree)


def _is_production_import(node: ast.ImportFrom, depth: int) -> bool:
    """`from .webhook_security import ...` as the integration's own module writes it: `depth` dots reach the package
    from the file (1 for a module of it, 2 for one in a subpackage), or the absolute path. A module of that name
    anywhere else is another module, whose function of that name may do nothing."""
    if node.level == 0:
        return node.module == PRODUCTION_MODULE
    return node.level == depth and node.module == "webhook_security"


def _bound_names(node: ast.AST, depth: int = 1) -> list[str]:
    """The names a node binds: a def or class, a parameter, a store, `except ... as`, a `case` capture, an import."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name or ""]
    if isinstance(node, ast.MatchMapping):
        return [node.rest or ""]  # `case {**name}`
    if isinstance(node, ast.Import):
        return [(alias.asname or alias.name).split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        production = _is_production_import(node, depth)  # its own redactor is the real one
        names = [
            alias.asname or alias.name for alias in node.names if not production or alias.name not in SAFE_WRAPPERS
        ]
        # `from other import *` may bind the redactor's name to anything
        return [*SAFE_WRAPPERS, *names] if "*" in names and not production else names
    return []


def _redactor_is_rebound(tree: ast.AST, depth: int = 1) -> bool:
    """Whether the module binds the redactor's name to anything but the production function (`from .webhook_security
    import redact_webhook_ids`). Then a call of that name is no proof that anything is redacted."""
    return any(name in SAFE_WRAPPERS for node in ast.walk(tree) for name in _bound_names(node, depth))


def _scan(source: str, depth: int = 1) -> list[tuple[str, int]]:
    """Passes over the module until the flow it learns (attributes, returns, arguments) stops growing; the hits of
    the last pass, which saw all of it, count. `depth`: how many dots a relative import needs to reach the package."""
    tree = _inline_path_field(ast.parse(source))
    module = _module(tree)
    flow = _Flow(shadowed=_redactor_is_rebound(tree, depth))
    while True:
        scan = _Scan(flow, module)
        scan.visit(tree)
        if not flow.absorb(scan.learned):
            return scan.hits


def _import_depth(rel: str) -> int:
    """How many dots a relative import needs to reach the integration package from a file (given relative to it)."""
    return len(rel.split("/"))


def test_a_file_reaches_the_package_with_one_dot_per_path_part() -> None:
    assert _import_depth("api.py") == 1
    assert _import_depth("services/misc.py") == 2
    assert _import_depth("a/b/c.py") == 3


def test_no_log_line_notification_repair_or_exception_carries_a_webhook_id_or_the_secret() -> None:
    found: Counter[tuple[str, str]] = Counter()
    lines: dict[tuple[str, str], list[int]] = {}
    for path in sorted(INTEGRATION.rglob("*.py")):
        rel = path.relative_to(INTEGRATION).as_posix()
        for scope, line in _scan(path.read_text(encoding="utf-8"), depth=_import_depth(rel)):
            found[rel, scope] += 1
            lines.setdefault((rel, scope), []).append(line)

    allowed = Counter({key: count for key, (count, _why) in ALLOWED.items()})
    unlisted = {f"{k[0]}::{k[1] or '<module>'} (lines {lines[k]})": n for k, n in (found - allowed).items()}
    stale = {f"{k[0]}::{k[1] or '<module>'}": f"{n} listed too many" for k, n in (allowed - found).items()}
    assert not unlisted, f"a webhook id or the secret may reach a log, a notification or an exception: {unlisted}"
    assert not stale, f"ALLOWED lists uses that no longer exist (lower the count or drop the entry): {stale}"


def test_the_scan_sees_an_id_where_it_is_printed_and_not_where_it_is_redacted_or_not_printed() -> None:
    source = """
def log_plain(value):
    _LOGGER.info("plain %s", value)  # leak


def module_field(cmd):  # defined before the constant: only the inlined field finds it
    _LOGGER.info("m %s", cmd[FIELD_OF_PATH])  # leak


FIELD_OF_PATH = "Parameter"


def shadowed(FIELD_OF_PATH, cmd):
    _LOGGER.info("s %s", cmd[FIELD_OF_PATH])


def kw_only(*rest, **kw):
    _LOGGER.info("kw %s", kw)  # leak


class Holder:
    def __init__(self, value):
        self._kept = value

    def show(self):
        _LOGGER.info("kept %s", self._kept)  # leak


class Bystander:
    def __init__(self, value):
        _LOGGER.info("bystander %s", value)

    def Record(self):
        return None


class Base:
    def __init__(self, value):
        self._based = value

    def based(self):
        _LOGGER.info("based %s", self._based)  # leak


class Derived(Base):
    pass


class Left:
    pass


class Right:
    def __init__(self, value):
        _LOGGER.info("right %s", value)  # leak


class Both(Base, Left, Right):
    pass


@dataclass
class Plain:
    inherited: str

    def show(self):
        _LOGGER.info("inherited %s", self.inherited)  # leak


@dataclass(frozen=True)
class Rich(Plain):
    own: str


class Child(Plain):
    pass


@dataclass
class Wrapped:
    wrapped: str

    def show(self):
        _LOGGER.info("wrapped %s", self.wrapped)  # leak


class Wrapper(Wrapped):
    def __init__(self, value):
        super().__init__(value)


class Foreign(ForeignBase):
    def __init__(self, value):
        super().__init__(value)


class Keeper:
    def __init__(self, value):
        _LOGGER.info("keeper %s", value)  # leak


@dataclass(init=False)
class Manual(Keeper):
    note: str


@dataclass
class Sub(Base):
    extra: str

    def show(self):
        _LOGGER.info("extra %s", self.extra)  # leak


@dataclass
class Record:
    recorded: str

    def show(self):
        _LOGGER.info("recorded %s", self.recorded)  # leak


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
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id!r}"))  # leak
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id:s}"))  # leak
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{secret}"))  # leak
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{self.secret}"))  # leak
        held = self.secret
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{held}"))  # leak
        notify = persistent_notification.async_create
        notify(hass, webhook_id)  # leak
        plog = functools.partial(_LOGGER.warning, "x")
        plog(webhook_id)  # leak
        if (got := webhook_id):
            _LOGGER.info("got %s", got)  # leak
        match cmd:
            case {"Parameter": captured}:
                _LOGGER.info("captured %s", captured)  # leak
            case {"Parameter": inner_path} as whole:
                _LOGGER.info("whole %s", whole)  # leak
            case {"Name": name}:
                _LOGGER.info("name %s", name)
        outer_id = webhook_id

        class Inner:
            outer_id = "safe in the class body only"

            def method(self):
                _LOGGER.info("method %s", outer_id)  # leak

        _LOGGER.info("outer %s", outer_id)  # leak
        log4 = log
        log4("copy %s", webhook_id)  # leak
        bound = functools.partial(log, webhook_id)  # leak
        from notifications import async_create as create_it

        create_it(hass, webhook_id)  # leak
        look = lambda shown=webhook_id: _LOGGER.info("l %s", shown)  # leak
        PATH_FIELD = "Parameter"
        _LOGGER.info("p %s", cmd[PATH_FIELD])  # leak
        later = webhook_id
        _ = "x" if flag else (later := "safe")
        _LOGGER.info("later %s", later)  # leak
        token = webhook_id

        def inner():
            _LOGGER.info("token %s", token)  # leak

        msg = "x"
        msg += webhook_id
        _LOGGER.info(msg)  # leak
        for value in (cmd["Parameter"],):
            _LOGGER.info(value)  # leak
        holder.value = webhook_id
        _LOGGER.info("holder %s", holder.value)  # leak
        self._held = webhook_id
        _LOGGER.info("held %s", self._held)  # leak
        box = []
        box.append(webhook_id)
        _LOGGER.info("box %s", box)  # leak
        table = {}
        table["k"] = webhook_id
        _LOGGER.info("table %s", table)  # leak
        [_LOGGER.info("each %s", each) for each in (webhook_id,)]  # leak
        with opened(cmd["Parameter"]) as handle:
            _LOGGER.info("handle %s", handle)  # leak
        _LOGGER.info("made %s", self.made())  # leak
        self._log_it(webhook_id)
        log_plain(cmd["Parameter"])
        self._show_hook(self.secret)
        _LOGGER.info("gen %s", next(self.gen()))  # leak
        _LOGGER.info("gen %s", list(self.gen_from()))  # leak
        first_id, *rest_ids = (webhook_id, 1)
        _LOGGER.info("rest %s", rest_ids)  # leak
        holder.webhook_id = self.secret
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{other.webhook_id}"))  # leak
        Holder(webhook_id).show()
        Record(recorded=webhook_id).show()
        hass.async_add_executor_job(self._log_cb, webhook_id)
        C._log_cls(self, webhook_id)
        self._log_if(webhook_id)
        kw_only(rest=webhook_id)
        Derived(webhook_id)
        Both(webhook_id)
        Sub(extra=webhook_id)
        Rich(webhook_id, "own")
        Child(inherited=webhook_id)
        Manual(webhook_id)
        Wrapper(webhook_id)
        Foreign(webhook_id)
        hass.async_add_executor_job(C._log_cls_cb, self, webhook_id)
        webhook_id = self.made()
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak
        webhook_id = self.secret
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak

    def made(self):
        return self.secret

    def gen(self):
        yield self.secret

    def gen_from(self):
        yield from (self.secret,)

    def _log_cb(self, value):
        _LOGGER.info("cb %s", value)  # leak

    def _log_cls(self, value):
        _LOGGER.info("cls %s", value)  # leak

    def _log_cls_cb(self, value):
        _LOGGER.info("cls cb %s", value)  # leak

    if FLAG:

        def _log_if(self, value):
            _LOGGER.info("if %s", value)  # leak

    def _log_it(self, shown):
        _LOGGER.info("helper %s", shown)  # leak

    def _show_hook(self, webhook_id):
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak

    def _show_safe_hook(self, webhook_id):
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))

    async def async_leaks(self, webhook_id):
        print(webhook_id)  # leak

    def defaults(self, shown=cmd["Parameter"], *, kept=webhook_id, fine="x"):
        _LOGGER.info("d %s", shown)  # leak
        _LOGGER.info("d %s", kept)  # leak
        _LOGGER.info("d %s", fine)

    def imported(self, webhook_id):
        from logging import warning as warn_it

        warn_it("x %s", webhook_id)  # leak

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
        mode = "Parameter"
        mode = "plain"
        _LOGGER.info("mode %s", mode)
        _LOGGER.info("api %s", api)
        for field_name in ("Name", "Title"):
            _LOGGER.info("field %s", field_name)
        PATH_FIELD = "Name"
        _LOGGER.info("p %s", cmd[PATH_FIELD])
        self._show_safe_hook(derive_webhook_id(secret, "k"))
        self.update(webhook_id)
        _LOGGER.info("self %s", self.name)
        register(webhook_id, self._quiet_cb, hass)

        def inner_safe():
            _LOGGER.info(redact_webhook_ids(hook))

    def _quiet_cb(self, hass_arg):
        _LOGGER.info("quiet %s", hass_arg)
"""
    hits = _scan(source)
    marked = [number for number, line in enumerate(source.splitlines(), 1) if line.endswith("# leak")]

    assert Counter(scope for scope, _line in hits) == {
        "log_plain": 1,
        "module_field": 1,
        "kw_only": 1,
        "Holder.show": 1,
        "Record.show": 1,
        "C.leaks": 53,
        "C.leaks.inner": 1,
        "C._log_it": 1,
        "C._show_hook": 1,
        "C._log_cb": 1,
        "C._log_cls": 1,
        "C._log_cls_cb": 1,
        "Base.based": 1,
        "Right.__init__": 1,
        "Sub.show": 1,
        "Plain.show": 1,
        "Keeper.__init__": 1,
        "Wrapped.show": 1,
        "C._log_if": 1,
        "C.leaks.Inner.method": 1,
        "C.leaks.<lambda>": 1,
        "C.async_leaks": 1,
        "C.defaults": 2,
        "C.imported": 1,
    }
    assert sorted(line for _scope, line in hits) == marked  # each marked line once, nothing else


def _argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((k.value for k in node.keywords if k.arg == keyword), None)


SECRET_FLOW = """
def path_for(cmd):
    return secret


def show_helper(cmd):
    _LOGGER.info(redact_webhook_ids(f"/api/webhook/{path_for(cmd)}"))  # leak


def stored_helper_result(cmd):
    webhook_id = path_for(cmd)
    _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak


def secret_default(webhook_id=secret):
    _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak


def box_holds_secret():
    webhook_id = []
    webhook_id.append(secret)
    _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak


@dataclass
class Keyed:
    webhook_id: str

    def show(self):
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{self.webhook_id}"))  # leak


@dataclass
class Idle:
    command_path: str

    def show(self):
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{self.command_path}"))


def build():
    Idle(derive_webhook_id(secret, "k"))
    return Keyed(secret)


def caught(webhook_id, session):
    try:
        session.get(f"/api/webhook/{webhook_id}")
    except OSError as err:
        _LOGGER.warning("failed %s", err)  # leak


def caught_secret(session):
    try:
        session.get(secret)
    except OSError as err:
        _LOGGER.warning("failed %s", err)  # leak


def caught_as_an_id_name(session):
    try:
        session.get(secret)
    except OSError as webhook_id:
        _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))  # leak


def caught_nothing(session, cmd):
    try:
        session.get("/api/state")
    except OSError as err:
        _LOGGER.warning("failed %s", err)


def caught_without_a_name(session, webhook_id, secret):
    try:
        session.post(webhook_id)
    except OSError:
        _LOGGER.exception("failed")  # leak
    try:
        session.post(webhook_id)
    except OSError:
        _LOGGER.warning("failed", exc_info=True)  # leak
    try:
        session.post(webhook_id)
    except OSError:
        traceback.print_exc()  # leak
    try:
        session.post(webhook_id)
    except OSError:
        text = traceback.format_exc()  # leak
    try:
        session.post(webhook_id)
    except OSError as err:
        _LOGGER.error("failed", exc_info=err)  # leak
    try:
        session.get(secret)
    except OSError:
        _LOGGER.exception("failed")  # leak
    try:
        session.post(webhook_id)
    except OSError:
        try:
            session.get("/api/state")
        except ValueError:
            _LOGGER.exception("nested")  # leak
    try:
        session.post(webhook_id)
    except OSError:
        _LOGGER.warning("failed", exc_info=False)
        _LOGGER.warning("failed")
    _LOGGER.exception("outside of any handler")
    try:
        session.post(webhook_id)
    finally:
        _LOGGER.exception("in finally")  # leak
    try:
        session.post(webhook_id)
    except OSError:
        options = {"exc_info": True}
        _LOGGER.error("failed", **options)  # leak
        session.get("/api/state", exc_info=True)
        session.get("/api/state")
    try:
        session.post(webhook_id)
    except OSError:
        pass
    else:
        _LOGGER.exception("in else: no exception is being handled")


def caught_no_id_without_a_name(session):
    try:
        session.get("/api/state")
    except OSError:
        _LOGGER.exception("failed")
        _LOGGER.warning("failed", exc_info=True)
        traceback.print_exc()


def streams(webhook_id, other):
    sys.stderr.write(webhook_id)  # leak
    sys.stdout.writelines([webhook_id])  # leak
    warnings.warn(webhook_id)  # leak
    warnings.warn_explicit(webhook_id, UserWarning, "f", 1)  # leak
    err.add_note(webhook_id)  # leak
    other.write(webhook_id)
    sys.stderr.write("fixed text")
"""


def test_the_scan_keeps_the_secret_apart_from_ids_and_follows_it_into_handlers_and_streams() -> None:
    hits = _scan(SECRET_FLOW)
    marked = [number for number, line in enumerate(SECRET_FLOW.splitlines(), 1) if line.endswith("# leak")]

    assert sorted(line for _scope, line in hits) == marked


_SHOW = 'def show(webhook_id):\n    _LOGGER.info(redact_webhook_ids(f"/api/webhook/{webhook_id}"))\n'


@pytest.mark.parametrize(
    ("source", "leaks"),
    [
        (f"from .webhook_security import redact_webhook_ids\n\n\n{_SHOW}", False),
        (f"from .webhook_security import redact_webhook_ids as _r\n\n\n{_SHOW}", False),
        (f"from custom_components.comexio.webhook_security import redact_webhook_ids\n\n\n{_SHOW}", False),
        (f"from .helpers import redact_webhook_ids\n\n\n{_SHOW}", True),
        # a module of that name somewhere else is not the production one, whatever its function does
        (f"from other.webhook_security import redact_webhook_ids\n\n\n{_SHOW}", True),
        (f"from webhook_security import redact_webhook_ids\n\n\n{_SHOW}", True),
        (f"from ..webhook_security import redact_webhook_ids\n\n\n{_SHOW}", True),  # one dot too many for this depth
        (f"from custom_components.other.webhook_security import redact_webhook_ids\n\n\n{_SHOW}", True),
        (f"from .webhook_security import other as redact_webhook_ids\n\n\n{_SHOW}", True),
        (f"import json as redact_webhook_ids\n\n\n{_SHOW}", True),
        (f"redact_webhook_ids = str\n\n\n{_SHOW}", True),
        (f"def redact_webhook_ids(text):\n    return text\n\n\n{_SHOW}", True),
        (f"class redact_webhook_ids:\n    pass\n\n\n{_SHOW}", True),
        (f"for redact_webhook_ids in (str,):\n    pass\n\n\n{_SHOW}", True),
        (f"from .helpers import *\n\n\n{_SHOW}", True),
        (f"from .webhook_security import *\n\n\n{_SHOW}", False),
        (f"from webhook_security import *\n\n\n{_SHOW}", True),
        # not the redactor: the call of another module's function of that name is no redaction either
        (_SHOW.replace("redact_webhook_ids(", "other.redact_webhook_ids("), True),
        (
            "import webhook_security\n\n\n"
            + _SHOW.replace("redact_webhook_ids(", "webhook_security.redact_webhook_ids("),
            True,
        ),
        (
            _SHOW.replace(
                "    _LOGGER", "    match {}:\n        case {**redact_webhook_ids}:\n            pass\n    _LOGGER"
            ),
            True,
        ),
        (_SHOW.replace("show(webhook_id)", "show(webhook_id, redact_webhook_ids)"), True),
        (_SHOW.replace("    _LOGGER", "    redact_webhook_ids = str\n    _LOGGER"), True),
    ],
)
def test_a_name_bound_to_something_else_than_the_production_redactor_is_not_trusted(source: str, leaks: bool) -> None:
    assert bool(_scan(source)) is leaks


def test_a_module_of_a_subpackage_reaches_the_production_redactor_with_one_dot_more() -> None:
    source = f"from ..webhook_security import redact_webhook_ids\n\n\n{_SHOW}"

    assert not _scan(source, depth=2)
    assert _scan(source, depth=1)
    assert _scan(f"from .webhook_security import redact_webhook_ids\n\n\n{_SHOW}", depth=2)


def _plain_get(call: ast.Call) -> bool:
    """`.get(key, "")`: the empty string is what WebhookSecurity takes as no host. Any other fallback (another
    constant, a name, a missing one that makes it None) would bind the ledger to a host the entry does not have."""
    fallback = call.args[1] if len(call.args) == 2 else None
    return not call.keywords and isinstance(fallback, ast.Constant) and fallback.value == ""


def _entry_host_read(host: ast.expr) -> str | None:
    """The entry whose host option `host` reads: `<entry>.data[CONF_HOST]` or `.data.get(CONF_HOST, "")`, in str()."""
    if isinstance(host, ast.Call) and _call_name(host) == "str" and len(host.args) == 1:
        host = host.args[0]
    if isinstance(host, ast.Subscript):
        data, key = host.value, host.slice
    elif isinstance(host, ast.Call) and isinstance(host.func, ast.Attribute) and host.func.attr == "get":
        if not _plain_get(host):
            return None
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
        ("WebhookSecurity(hass, entry.entry_id, sid, str(entry.data.get(CONF_HOST, other.data[CONF_HOST])))", False),
        ("WebhookSecurity(hass, entry.entry_id, sid, entry.data.get(CONF_HOST, other_host))", False),
        ('WebhookSecurity(hass, entry.entry_id, sid, entry.data.get(CONF_HOST, default=""))', False),
        ('WebhookSecurity(hass, entry.entry_id, sid, entry.data.get(CONF_HOST, "", 1))', False),
        ('WebhookSecurity(hass, entry.entry_id, sid, entry.data.get(CONF_HOST, "other.example"))', False),
        ("WebhookSecurity(hass, entry.entry_id, sid, entry.data.get(CONF_HOST, None))", False),
        ("WebhookSecurity(hass, entry.entry_id, sid, str(entry.data.get(CONF_HOST)))", False),
        ("WebhookSecurity(hass, entry.entry_id, sid, str(entry.data.get(CONF_HOST, 0)))", False),
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


def _constructor_names(tree: ast.AST) -> frozenset[str]:
    """`WebhookSecurity` and the names it is imported as (`from .webhook_security import WebhookSecurity as WS`)."""
    names = {SECURITY_CLASS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.asname for alias in node.names if alias.asname and alias.name == SECURITY_CLASS}
    return frozenset(names)


def _annotations(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.arg) and node.annotation:
        return [node.annotation]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
        return [node.returns]
    return [node.annotation] if isinstance(node, ast.AnnAssign) else []


def _loose_references(tree: ast.AST, names: frozenset[str]) -> list[int]:
    """Lines that use the security class other than by calling it or naming it in an annotation: an alias, a partial,
    a factory argument - a construction the host check could not see."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            allowed.add(id(node.func))
        allowed |= {id(part) for annotation in _annotations(node) for part in ast.walk(annotation)}
    return [
        node.lineno
        for node in ast.walk(tree)
        if (
            (isinstance(node, ast.Name) and node.id in names)
            or (isinstance(node, ast.Attribute) and node.attr in names)
        )
        and id(node) not in allowed
    ]


@pytest.mark.parametrize(
    ("source", "loose"),
    [
        ("security = WebhookSecurity(hass, e.entry_id, s, h)", []),
        ("security = mod.WebhookSecurity(hass, e.entry_id, s, h)", []),
        ("from a import WebhookSecurity as WS\nsecurity = WS(hass, e.entry_id, s, h)", []),
        ("def f(s: WebhookSecurity) -> WebhookSecurity | None: ...", []),
        ("held: WebhookSecurity | None = None", []),
        ("W = WebhookSecurity", [1]),
        ("W: type = WebhookSecurity", [1]),
        ("(W := WebhookSecurity)", [1]),
        ("W = mod.WebhookSecurity", [1]),
        ("build = partial(WebhookSecurity, hass)", [1]),
        ("from a import WebhookSecurity as WS\nW = WS", [2]),
        ("isinstance(x, WebhookSecurity)", [1]),  # strict on purpose: no use the check cannot place
        ("class Sub(WebhookSecurity): ...", [1]),
    ],
)
def test_the_security_class_is_only_called_or_named_in_an_annotation(source: str, loose: list[int]) -> None:
    tree = ast.parse(source)

    assert _loose_references(tree, _constructor_names(tree)) == loose


def test_every_webhook_security_is_built_with_the_host_of_its_entry() -> None:
    problems: list[str] = []
    built = 0
    for path in sorted(INTEGRATION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _constructor_names(tree)
        problems += [
            f"{path.name}:{line}: {SECURITY_CLASS} used other than called" for line in _loose_references(tree, names)
        ]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in names:
                built += 1
                if problem := _host_problem(node):
                    problems.append(f"{path.name}:{node.lineno}: {problem}")
    assert built, "no WebhookSecurity(...) found: the check no longer looks where the entry builds it"
    assert not problems, f"WebhookSecurity must be given the host of its own entry: {problems}"
