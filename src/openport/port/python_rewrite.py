"""Rewrite Python code onto the OpenPort runtime: swap SDK clients, and point each ported call at its Nemotron model.
Plain HTTP calls (requests, httpx, aiohttp) move to Token Factory's URL and key instead (see http.py)."""

import ast
import codecs
import posixpath
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable

from ..runtime.openport_llm import _PROVIDER_ONLY_OPTIONS
from ..scan import _source_files
from ..scan.findings import CALL_SUFFIXES, CallSiteFinding, is_test_path
from ..scan.python_scanner import HTTP_METHODS, _constructor_sdk, _match_suffix, _module_name
from .clients import ClientAction, decide_client_actions
from .edits import Edit, apply_edits, before_newline, indentation, line_end, line_starts, newline_style, quoted_like, was_comment
from .http import BASE_URL_VARIABLE, BASE_URL_VARIABLES, CHAT_URL, KEY_VARIABLE, TOKEN_FACTORY_V1, url_edits, why_kept
from .plan import SiteDecision
from .prompts import find_prompt_file, prompt_file_insertion, python_literal_insertion

RUNTIME_MODULE = "openport_llm"
HELPERS = {
    "OpenAI": "token_factory_client",
    "AzureOpenAI": "token_factory_client",
    "AsyncOpenAI": "token_factory_async_client",
    "AsyncAzureOpenAI": "token_factory_async_client",
    "Anthropic": "anthropic_client",
    "AnthropicBedrock": "anthropic_client",
    "AnthropicVertex": "anthropic_client",
    "AsyncAnthropic": "async_anthropic_client",
    "AsyncAnthropicBedrock": "async_anthropic_client",
    "AsyncAnthropicVertex": "async_anthropic_client",
}
SDK_MODULES = ("openai", "anthropic")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class _File:
    path: str
    source: bytes
    tree: ast.Module
    starts: list[int]
    parents: dict[int, ast.AST]

    @classmethod
    def parse(cls, root: Path, path: str) -> "_File":
        source = (root / path).read_bytes()
        tree = ast.parse(source)
        parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        starts = line_starts(source)
        if source.startswith(codecs.BOM_UTF8):  # ast counts line 1's columns from after a byte order mark
            starts[0] = len(codecs.BOM_UTF8)
        return cls(path, source, tree, starts, parents)

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.starts[node.lineno - 1] + node.col_offset,
            self.starts[node.end_lineno - 1] + node.end_col_offset,
        )

    def text(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.source[start:end].decode("utf-8", errors="replace")

    def ancestors(self, node: ast.AST) -> list[ast.AST]:
        chain = []
        while id(node) in self.parents:
            node = self.parents[id(node)]
            chain.append(node)
        return chain


@dataclass
class _Binding:
    """Where a client is constructed, and what name it's bound to."""

    file: str
    node: ast.Call
    ctor: str
    kind: str  # module | local | attribute | return | other
    var: str | None
    function: str | None  # qualified name of the enclosing function
    cls: str | None  # nearest enclosing class
    statement: ast.stmt
    top_level: bool  # built by a module-level statement


class _Repo:
    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, str] = {}
        for path in _source_files(root):
            if path.suffix == ".py":
                relative = path.relative_to(root).as_posix()
                self.modules.setdefault(_module_name(relative), relative)
        self._files: dict[str, _File | None] = {}
        self._bindings: dict[str, list[_Binding]] = {}
        self._sources: dict[str, bytes] = {}
        self._mentions: dict[str, list[_File]] = {}

    def file(self, path: str | None) -> _File | None:
        if not path:
            return None
        if path not in self._files:
            try:
                self._files[path] = _File.parse(self.root, path)
            except (SyntaxError, ValueError, OSError):
                self._files[path] = None
        return self._files[path]

    def bindings(self, f: _File) -> list[_Binding]:
        if f.path not in self._bindings:
            self._bindings[f.path] = [self._binding(f, node) for node in ast.walk(f.tree) if _constructor_sdk(node)]
        return self._bindings[f.path]

    @staticmethod
    def _binding(f: _File, node: ast.Call) -> _Binding:
        chain = f.ancestors(node)
        statement = next(a for a in chain if isinstance(a, ast.stmt))
        scopes = [a for a in reversed(chain) if isinstance(a, (ast.ClassDef, *_FUNCTIONS))]
        functions = [a for a in chain if isinstance(a, _FUNCTIONS)]
        classes = [a for a in chain if isinstance(a, ast.ClassDef)]
        function = None
        if functions:
            names = []
            for scope in scopes:
                names.append(scope.name)
                if scope is functions[0]:
                    break
            function = ".".join(names)

        kind, var = "other", None
        value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Return)) else None
        # `client = OpenAI()`, or wrapped by the app or a library on the way: `client = instrument(OpenAI())`.
        built = value is node or isinstance(value, ast.Call) and any(arg is node for arg in value.args)
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and built:
            target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
            if isinstance(target, ast.Name):
                var = target.id
                declared_global = bool(functions) and any(
                    isinstance(s, ast.Global) and var in s.names for s in ast.walk(functions[0])
                )
                kind = "module" if not functions or declared_global else "local"
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id in ("self", "cls"):
                kind, var = "attribute", target.attr
        elif isinstance(statement, ast.Return) and built:
            kind = "return"
        ctor = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        return _Binding(f.path, node, ctor, kind, var, function, classes[0].name if classes else None, statement, not scopes)

    def module_path(self, module: str, importer: str) -> str | None:
        """Where an app module lives: by its dotted name from the repo root, or from the folder of the project the
        importing file is in (in a monorepo, platform/backend/app.py imports `backend.util` from platform/backend)."""
        if module in self.modules:
            return self.modules[module]
        parts = _module_name(importer).split(".")
        for end in range(len(parts) - 1, 0, -1):
            candidate = ".".join([*parts[:end], module])
            if candidate in self.modules:
                return self.modules[candidate]
        return None

    def imports(self, f: _File) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """(from-imported name -> (module, original name), module alias -> module)."""
        module = _module_name(f.path).split(".")
        package = module if f.path.endswith("__init__.py") else module[:-1]
        names, modules = {}, {}
        for node in ast.walk(f.tree):
            if isinstance(node, ast.ImportFrom):
                base = package[: len(package) - node.level + 1] if node.level else []
                full = ".".join(base + ([node.module] if node.module else []))
                for alias in node.names:
                    names[alias.asname or alias.name] = (full, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        modules[alias.asname] = alias.name
                    else:
                        modules[alias.name.split(".")[0]] = alias.name.split(".")[0]
        return names, modules

    def resolve_callable(self, f: _File, dotted: str, caller: str) -> tuple[str, str] | None:
        """Where a factory like `get_client`, `clients.get_client` or `self._client` is defined: (file, qualname)."""
        parts = dotted.split(".")
        names, modules = self.imports(f)
        if parts[0] in ("self", "cls") and len(parts) == 2 and "." in caller:
            return f.path, f"{caller.rsplit('.', 1)[0]}.{parts[1]}"
        if len(parts) == 1:
            if parts[0] in names:
                module, original = names[parts[0]]
                found = self.module_path(module, f.path)
                return (found, original) if found else None
            if any(isinstance(n, _FUNCTIONS) and n.name == parts[0] for n in f.tree.body):
                return f.path, parts[0]
            return None
        head, *middle, function = parts
        if head in modules:
            module = ".".join([modules[head], *middle])
        elif head in names:
            module = ".".join([*names[head], *middle])
        else:
            return None
        found = self.module_path(module, f.path)
        return (found, function) if found else None

    def link(self, site: CallSiteFinding) -> list[_Binding]:
        """The constructor(s) that build the client this call site uses."""
        f = self.file(site.file)
        if f is None:
            return []
        receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
        kind = site.receiver_kind
        if receiver != site.receiver:  # `client.beta`: the scanner saw an attribute; the client is what it's read from
            kinds = (["attribute"] if receiver.startswith(("self.", "cls.")) else ["factory"] if receiver.endswith(")")
                     else ["local", "module_global", "imported", "other"])
            return next((found for k in kinds if (found := self.link(replace(site, receiver=receiver, receiver_kind=k)))), [])
        options = re.fullmatch(r"([\w.]+?)\.with_options\(.*\)", receiver, re.S)
        if options:  # client.with_options(timeout=...): the SDK's copy of `client`, with other request options
            base = options[1]
            kinds = ["attribute"] if base.startswith(("self.", "cls.")) else ["local", "module_global", "imported", "other"]
            return next((found for k in kinds if (found := self.link(replace(site, receiver=base, receiver_kind=k)))), [])
        if kind == "module_global":
            return [b for b in self.bindings(f) if b.kind == "module" and b.var == receiver]
        if kind == "local":
            return [b for b in self.bindings(f) if b.kind == "local" and b.var == receiver and b.function == site.function]
        if kind == "attribute":  # set by the class the call is in (even from a function inside a method), or one it extends here
            match = re.fullmatch(r"(?:self|cls)\.(\w+)", receiver)
            call = _call_node(f, site)
            enclosing = next((a for a in f.ancestors(call) if isinstance(a, ast.ClassDef)), None) if call else None
            classes = _lineage(f, enclosing)
            return [b for b in self.bindings(f) if match and b.kind == "attribute" and b.var == match[1] and b.cls in classes]
        if kind == "imported":
            names, _ = self.imports(f)
            module, original = names.get(receiver, ("", ""))
            target = self.file(self.module_path(module, f.path))
            return [b for b in self.bindings(target) if b.kind == "module" and b.var == original] if target else []
        if kind == "factory":
            match = re.fullmatch(r"([\w.]+)\(.*\)", receiver, re.S)
            return self.made_by(f, match[1], site.function) if match else []
        if kind == "other" and re.fullmatch(r"\w+", receiver):  # `client = get_client(...)` earlier in the function
            wrapped = [b for b in self.bindings(f) if b.var == receiver  # client = instrument(OpenAI()), here or at the top
                       and (b.kind == "local" and b.function == site.function or b.kind == "module")]
            if wrapped:
                return wrapped
            call = _call_node(f, site)
            function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None) if call else None
            value = _assigned_value(function, receiver, site.line) if function else None
            value = value.value if isinstance(value, ast.Await) else value
            if isinstance(value, ast.Call) and not _constructor_sdk(value) and re.fullmatch(r"[\w.]+", ast.unparse(value.func)):
                return self.made_by(f, ast.unparse(value.func), site.function)
        return []

    def handed_in(self, site: CallSiteFinding) -> tuple[list[_Binding], bool]:
        """The constructors behind a client the call is handed: what its function's callers pass for that parameter,
        or what code building its object passes `__init__` (`self.client = client`), followed up to _HOPS calls back.
        Complete (True) only when every call the app makes (its tests don't count) passes a client the port can find;
        otherwise the clients it could find, which the call still uses wherever it stays."""
        f = self.file(site.file)
        call = _call_node(f, site) if f else None
        function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None) if call else None
        if function is None:
            return [], False
        receiver = _receiver_base(site.receiver)
        if receiver in _parameters(function):
            return self._passed(f, function, receiver, _HOPS)
        attribute = re.fullmatch(r"(?:self|cls)\.(\w+)", receiver)
        cls = next((a for a in f.ancestors(call) if isinstance(a, ast.ClassDef)), None)
        init = next((n for n in cls.body if isinstance(n, _FUNCTIONS) and n.name == "__init__"), None) if attribute and cls else None
        for node in ast.walk(init) if init else []:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in _parameters(init)
                    and any(isinstance(t, ast.Attribute) and t.attr == attribute[1] and isinstance(t.value, ast.Name)
                            and t.value.id == "self" for t in node.targets)):
                return self._passed(f, init, node.value.id, _HOPS)
        return [], False

    def _passed(self, f: _File, function: ast.AST, parameter: str, hops: int) -> tuple[list[_Binding], bool]:
        """The constructors behind what the calls of `function` pass for `parameter`, and whether that's all of them."""
        calls, complete = self._calls(f, function)
        found: list[_Binding] = []
        for caller_file, call, scope in calls:
            value = _argument(call, function, parameter)
            bindings, traced = self._value_bindings(caller_file, value, scope, hops - 1) if value is not None else ([], False)
            found += [b for b in bindings if b not in found]
            complete = complete and traced and bool(bindings)
        return found, complete and bool(calls)

    def _calls(self, f: _File, function: ast.AST) -> tuple[list[tuple[_File, ast.Call, ast.AST | None]], bool]:
        """The calls the app (not its tests) makes of a function, of a method through `self`, or, for `__init__`, of
        its class: (file, call, the function the call is in), and whether those are all. They aren't when it's also
        used some other way, like passed as a callback, where a call the port can't see might hand it another client."""
        cls = next((a for a in f.ancestors(function) if isinstance(a, ast.ClassDef)), None)
        if cls is not None and function.name != "__init__":  # a method: `self.method(...)` in its own class
            uses = [node for other in self._mentioning(function.name) for node in ast.walk(other.tree)
                    if isinstance(node, ast.Attribute) and node.attr == function.name]
            inside = {id(node) for node in ast.walk(cls)}
            calls = [(f, f.parents[id(node)], _scope(f, node)) for node in uses if id(node) in inside
                     and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
                     and isinstance(f.parents.get(id(node)), ast.Call) and f.parents[id(node)].func is node]
            return calls, len(calls) == len(uses)  # also called on an object from elsewhere: can't tell with what
        defined = cls if cls is not None else function
        if defined not in f.tree.body:  # a function inside another one: out of reach
            return [], False
        calls, complete = [], True
        for other in self._mentioning(defined.name):
            names, modules = self.imports(other)
            direct = {defined.name} if other.path == f.path else set()
            direct |= {local for local, (module, original) in names.items()
                       if original == defined.name and self.module_path(module, other.path) == f.path}
            aliases = {alias for alias, module in modules.items() if self.module_path(module, other.path) == f.path}
            aliases |= {local for local, (module, original) in names.items() if self.module_path(f"{module}.{original}", other.path) == f.path}
            for node in ast.walk(other.tree):
                if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in direct
                        or isinstance(node, ast.Attribute) and node.attr == defined.name and isinstance(node.value, ast.Name)
                        and node.value.id in aliases):
                    continue
                parent = other.parents.get(id(node))
                if isinstance(parent, ast.Call) and parent.func is node:
                    calls.append((other, parent, _scope(other, node)))
                elif cls is None or not _harmless_class_use(other, node):  # a class in an annotation or isinstance() is fine
                    complete = False
        return calls, complete

    def _mentioning(self, name: str) -> list[_File]:
        """The app's Python files (not its tests) whose source has `name` in it, parsed."""
        if name not in self._mentions:
            needle = name.encode()
            paths = [path for path in self.modules.values() if not is_test_path(path) and needle in self._raw(path)]
            self._mentions[name] = [f for f in map(self.file, paths) if f is not None]
        return self._mentions[name]

    def _raw(self, path: str) -> bytes:
        if path not in self._sources:
            try:
                self._sources[path] = (self.root / path).read_bytes()
            except OSError:
                self._sources[path] = b""
        return self._sources[path]

    def _value_bindings(self, f: _File, value: ast.AST, scope: ast.AST | None, hops: int) -> tuple[list[_Binding], bool]:
        """The constructors a client expression refers to, where it's written: a client built right there (or wrapped
        on the way), a factory's, a local, a module or imported client, `self.x`, or what the callers pass when it's a
        parameter. And whether that's the whole story: False when part of it can't be told."""
        value = value.value if isinstance(value, ast.Await) else value
        if isinstance(value, ast.Call):
            built = [b for b in self.bindings(f) if b.node is value or any(arg is b.node for arg in value.args)]
            if built:
                return built, True
            dotted = ast.unparse(value.func)
            made = self.made_by(f, dotted, _qualname(f, scope)) if re.fullmatch(r"[\w.]+", dotted) else []
            return made, bool(made)
        if isinstance(value, ast.Name):
            if scope is not None and value.id in _parameters(scope):
                return self._passed(f, scope, value.id, hops) if hops > 0 else ([], False)
            if scope is not None and (assigned := _assignments(scope, value.id, value.lineno)):
                found = [self._value_bindings(f, each, scope, hops) for each in assigned]  # whichever branch ran
                return [b for bs, _ in found for b in bs], all(traced and bs for bs, traced in found)
            module_level = [b for b in self.bindings(f) if b.kind == "module" and b.var == value.id]
            if module_level:
                return module_level, True
            names, _ = self.imports(f)
            if value.id in names:
                module, original = names[value.id]
                target = self.file(self.module_path(module, f.path))
                found = [b for b in self.bindings(target) if b.kind == "module" and b.var == original] if target else []
                return found, bool(found)
            assigned = _module_value(f, value.id)
            return self._value_bindings(f, assigned, None, hops) if assigned is not None else ([], False)
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            owner = value.value.id
            if owner in ("self", "cls"):
                cls = next((a for a in f.ancestors(scope) if isinstance(a, ast.ClassDef)), None) if scope is not None else None
                classes = _lineage(f, cls)
                found = [b for b in self.bindings(f) if b.kind == "attribute" and b.var == value.attr and b.cls in classes]
                return found, bool(found)
            names, modules = self.imports(f)  # `settings.client`, a client another module builds
            module = modules.get(owner) or (".".join(names[owner]) if owner in names else None)
            target = self.file(self.module_path(module, f.path)) if module else None
            found = [b for b in self.bindings(target) if b.kind == "module" and b.var == value.attr] if target else []
            return found, bool(found)
        return [], False

    def made_by(self, f: _File, dotted: str, caller: str) -> list[_Binding]:
        """The constructors a factory (`get_client`, `clients.get_client`, `self._client_for`) builds its clients with."""
        resolved = self.resolve_callable(f, dotted, caller)
        target = self.file(resolved[0]) if resolved else None
        if target is None:
            return []
        function = resolved[1]
        inside = [b for b in self.bindings(target) if b.function == function]
        if inside:
            return inside
        # A factory that hands back a client built at module level: `return _client`.
        returned = {
            ret.value.id
            for node in ast.walk(target.tree)
            if isinstance(node, _FUNCTIONS) and node.name == function.split(".")[-1]
            for ret in ast.walk(node)
            if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name)
        }
        return [b for b in self.bindings(target) if b.kind == "module" and b.var in returned]


_HOPS = 3  # how many calls back a handed-in client is followed


def _parameters(function: ast.AST) -> set[str]:
    args = function.args
    return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]} - {"self", "cls"}


def _scope(f: _File, node: ast.AST) -> ast.AST | None:
    """The function a node is in, or None at module level."""
    return next((a for a in f.ancestors(node) if isinstance(a, _FUNCTIONS)), None)


def _qualname(f: _File, function: ast.AST | None) -> str:
    """"Class.method", the way bindings name the function they're in."""
    if function is None:
        return ""
    scopes = [a for a in reversed(f.ancestors(function)) if isinstance(a, (ast.ClassDef, *_FUNCTIONS))]
    return ".".join([*(s.name for s in scopes), function.name])


def _argument(call: ast.Call, function: ast.AST, parameter: str) -> ast.AST | None:
    """What a call passes for one of a function's parameters; None when it passes nothing or the port can't tell
    (a `*args` or `**kwargs` spread in the way)."""
    for keyword in call.keywords:
        if keyword.arg == parameter:
            return keyword.value
    positional = [a.arg for a in [*function.args.posonlyargs, *function.args.args]]
    if positional[:1] in (["self"], ["cls"]):
        positional = positional[1:]
    if parameter not in positional:
        return None
    index = positional.index(parameter)
    if any(isinstance(a, ast.Starred) for a in call.args[: index + 1]) or index >= len(call.args):
        return None
    return call.args[index]


def _assignments(scope: ast.AST, name: str, before_line: int) -> list[ast.AST]:
    """Every value assigned to `name` in a function before a line: all must check out, whichever branch ran."""
    return [node.value for node in ast.walk(scope) if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            and node.lineno < before_line
            and any(isinstance(t, ast.Name) and t.id == name for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]


def _harmless_class_use(f: _File, node: ast.AST) -> bool:
    """A class named in a type annotation, or in isinstance() or issubclass(): it isn't built there."""
    for ancestor in [node, *f.ancestors(node)]:
        parent = f.parents.get(id(ancestor))
        if isinstance(parent, ast.arg) and parent.annotation is ancestor or isinstance(parent, ast.AnnAssign) and parent.annotation is ancestor:
            return True
        if isinstance(parent, _FUNCTIONS) and parent.returns is ancestor:
            return True
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in ("isinstance", "issubclass") and ancestor in parent.args:
            return True
        if isinstance(parent, ast.stmt):
            return False
    return False


def _chosen_by_caller(f: _File, binding: _Binding) -> str | None:
    """How a client lets whoever builds it pick the provider: its base URL (or all its options) comes from the
    function's parameters, its locals, or the object's attributes. None when the code fixes it (no base URL, a
    literal, an environment variable, a module constant or the app's config)."""
    function = next((a for a in f.ancestors(binding.node) if isinstance(a, _FUNCTIONS)), None)
    if function is None:
        return None
    args = function.args
    params = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a} - {"self", "cls"}

    def from_caller(value: ast.AST, depth: int = 2) -> bool:  # locals are followed to what they're assigned
        for n in ast.walk(value):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"):
                return True
            if isinstance(n, ast.Name) and (n.id in params or depth and (assigned := _assigned_value(function, n.id, binding.node.lineno))
                                            is not None and from_caller(assigned, depth - 1)):
                return True
        return False

    for keyword in binding.node.keywords:
        if keyword.arg in (None, "base_url") and from_caller(keyword.value):
            return f"`base_url={ast.unparse(keyword.value)}`" if keyword.arg else f"`**{ast.unparse(keyword.value)}`"
    return None


def _receiver_base(receiver: str) -> str:
    """The client a call's receiver names: without `.beta`, or `.with_options(...)` (the SDK's copy of a client)."""
    return re.sub(r"(?:\.beta)+$", "", re.sub(r"\.with_options\(.*\)$", "", receiver, flags=re.S))


def _foreign_client(f: _File | None, site: CallSiteFinding) -> str | None:
    """Why a client the port didn't find stays: it's another class (`client = Groq()`, `self.client = ZhipuAI()`),
    or the object was handed it when built (`self.client = client` in `__init__`). None when neither."""
    call = _call_node(f, site) if f else None
    if call is None:
        return None
    receiver = _receiver_base(site.receiver)
    function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None)
    cls = next((a for a in f.ancestors(call) if isinstance(a, ast.ClassDef)), None)
    attribute = re.fullmatch(r"(?:self|cls)\.(\w+)", receiver)
    assigned: list[tuple[ast.AST, ast.AST]] = []  # (value, the function it's assigned in)
    if attribute and cls is not None:
        for method in (n for n in cls.body if isinstance(n, _FUNCTIONS)):
            assigned += [(n.value, method) for n in ast.walk(method) if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == attribute[1] and isinstance(t.value, ast.Name) and t.value.id in ("self", "cls")
                for t in n.targets)]
    elif re.fullmatch(r"\w+", receiver) and function is not None:
        value = _assigned_value(function, receiver, site.line)
        assigned = [(value, function)] if value is not None else []
    for value, scope in assigned:
        value = value.value if isinstance(value, ast.Await) else value
        name = value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else \
            value.func.attr if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) else ""
        if name[:1].isupper() and not _constructor_sdk(value):
            return f"its client is a `{name}`, not one of OpenAI's or Anthropic's SDK clients: port it by hand"
        params = {a.arg for a in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]}
        if isinstance(value, ast.Name) and value.id in params:
            return (f"its client is whatever builds the object passes (the `{value.id}` parameter of `{scope.name}`): "
                    "port the code that builds that client instead")
    if attribute and cls is not None and not assigned and cls.bases:  # set up by a class from elsewhere
        return f"its client is set up by the class it extends (`{ast.unparse(cls.bases[0])}`): port it there, or by hand"
    return None


def _parameter_client(f: _File | None, site: CallSiteFinding) -> str | None:
    """The parameter a call's client arrives through, when it's a parameter of the function the call is in."""
    receiver = _receiver_base(site.receiver)
    call = _call_node(f, site) if f else None
    function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None) if call else None
    if function is None or not re.fullmatch(r"\w+", receiver):
        return None
    args = function.args
    return receiver if receiver in {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]} else None


def _providers(bindings: list[_Binding]) -> list[str]:
    """The base URLs a call's clients are built with, when it gets one of several: one each means a choice of provider."""
    def base_url(binding: _Binding) -> str:
        keyword = next((k for k in binding.node.keywords if k.arg == "base_url"), None)
        return ast.unparse(keyword.value) if keyword else "the SDK's default"

    return sorted({base_url(b) for b in bindings}) if len(bindings) > 1 else []


def _lineage(f: _File, cls: ast.ClassDef | None) -> set[str]:
    """A class's name and the names of the classes it extends that this file defines, all the way up."""
    defined = {node.name: node for node in f.tree.body if isinstance(node, ast.ClassDef)}
    names, pending = set(), [cls] if cls is not None else []
    while pending:
        current = pending.pop()
        if current.name in names:
            continue
        names.add(current.name)
        for base in current.bases:
            name = base.id if isinstance(base, ast.Name) else base.value.id if isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name) else None
            if name in defined:
                pending.append(defined[name])
    return names


def _shadowed(f: _File, node: ast.Name) -> bool:
    """Whether a name is the function's own local there, not the module's."""
    function = _scope(f, node)
    if function is None:
        return False
    if any(isinstance(n, ast.Global) and node.id in n.names for n in ast.walk(function)):
        return False
    args = function.args
    return node.id in {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a} or any(
        isinstance(n, ast.Name) and n.id == node.id and isinstance(n.ctx, ast.Store) for n in ast.walk(function))


def _importers(repo: _Repo, f: _File, name: str) -> list[tuple[_File, ast.AST]]:
    """Where the app's other modules (not its tests) import a module-level name of `f`: each `from f import name`
    (read or not), and each read of it, under that name or as `f.name` through the module."""
    found = []
    for other in repo._mentioning(name):
        if other.path == f.path:
            continue
        names, modules = repo.imports(other)
        direct = {local for local, (module, original) in names.items() if original == name and repo.module_path(module, other.path) == f.path}
        aliases = {alias for alias, module in modules.items() if repo.module_path(module, other.path) == f.path}
        aliases |= {local for local, (module, original) in names.items() if repo.module_path(f"{module}.{original}", other.path) == f.path}
        for node in ast.walk(other.tree):
            if (isinstance(node, ast.ImportFrom) and any(a.name == name and (a.asname or a.name) in direct for a in node.names)
                    or isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in direct
                    or isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.value, ast.Name) and node.value.id in aliases):
                found.append((other, node))
    return found


def _client_references(repo: _Repo, binding: _Binding) -> list[tuple[_File, ast.AST]]:
    """The expressions in the app (not its tests) that are a client: its variable, `self.client` in its class, a
    module-level client where other modules import it, and calls of a top-level factory that returns it."""
    f = repo.file(binding.file)
    found: list[tuple[_File, ast.AST]] = []
    if binding.kind == "module" and binding.var:
        found += [(f, n) for n in ast.walk(f.tree) if isinstance(n, ast.Name) and n.id == binding.var
                  and isinstance(n.ctx, ast.Load) and not _shadowed(f, n)]
        found += [(other, n) for other, n in _importers(repo, f, binding.var) if not isinstance(n, ast.ImportFrom)]
    elif binding.kind == "local" and binding.var:
        function = _scope(f, binding.node)
        found += [(f, n) for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == binding.var and isinstance(n.ctx, ast.Load)]
    elif binding.kind == "attribute" and binding.var:
        classes = [c for c in ast.walk(f.tree) if isinstance(c, ast.ClassDef) and binding.cls in _lineage(f, c)]
        found += [(f, n) for c in classes for n in ast.walk(c) if isinstance(n, ast.Attribute) and n.attr == binding.var
                  and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls") and isinstance(n.ctx, ast.Load)]

    def returns_client(function: ast.AST) -> bool:
        if binding.kind == "local" and binding.function != function.name:
            return False
        return any(isinstance(r, ast.Return) and r.value is not None and (r.value is binding.node or binding.var is not None
                   and isinstance(r.value, ast.Name) and r.value.id == binding.var) for r in ast.walk(function))

    for factory in (n for n in f.tree.body if isinstance(n, _FUNCTIONS) and returns_client(n)):
        found += [(other, call) for other, call, _ in repo._calls(f, factory)[0]]
    return found


def _attribute_chain(f: _File, node: ast.AST) -> tuple[list[str], ast.AST]:
    """The attributes read off a client expression, through `.with_options(...)` (the SDK's copy of the client) and
    `.beta`: ["audio", "transcriptions", "create"] for `client.audio.transcriptions.create`, and the outermost node."""
    names: list[str] = []
    current = node
    while True:
        parent = f.parents.get(id(current))
        if isinstance(parent, ast.Attribute) and parent.value is current:
            names.append(parent.attr)
        elif isinstance(parent, ast.Call) and parent.func is current and names[-1:] == ["with_options"]:
            names.pop()
        else:
            return [n for i, n in enumerate(names) if n != "beta" or any(m != "beta" for m in names[:i])], current
        current = parent


def _other_uses(repo: _Repo, binding: _Binding, linked: Callable[[str, int], bool]) -> list[str]:
    """What the app does with a client besides the calls the port linked to it: `client.models.list()`,
    `client.audio.transcriptions.create(...)`, `client.with_raw_response...`, or a call the port couldn't trace to it.
    None of that moves to Token Factory, so each counts as a call that stays: the client is split, or kept.
    `linked(file, line)` says whether the call on that line is one of the client's own."""
    found: list[str] = []
    pending, aliased = _client_references(repo, binding), False
    while pending:
        aliases: list[tuple[_File, ast.AST]] = []
        for f, node in pending:
            names, outer = _attribute_chain(f, node)
            parent = f.parents.get(id(outer))
            if not names:  # `fast = client.with_options(timeout=5)`: the same client, under another name
                if not aliased and isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                    scope = _scope(f, parent) or f.tree
                    aliases += [(f, n) for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == parent.targets[0].id
                                and isinstance(n.ctx, ast.Load)]
                continue
            called = isinstance(parent, ast.Call) and parent.func is outer
            if called and any(tuple(names) == suffix for suffix, _, _ in CALL_SUFFIXES) and linked(f.path, parent.lineno):
                continue
            use = f"`{ast.unparse(outer)}` ({f.path}:{outer.lineno})"
            if use not in found:
                found.append(use)
        pending, aliased = aliases, True
    return found


def _read_only_at(repo: _Repo, f: _File, name: str, uses: list[ast.AST]) -> bool:
    """Whether a module-level name is read nowhere but at `uses`: not elsewhere in its file (a tokenizer, a log line),
    and not imported by another module of the app."""
    used = {id(node) for node in uses}
    if any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load) and id(n) not in used for n in ast.walk(f.tree)):
        return False
    return not _importers(repo, f, name)


def _call_node(f: _File, site: CallSiteFinding) -> ast.Call | None:
    for node in ast.walk(f.tree):
        if isinstance(node, ast.Call) and node.lineno == site.line:
            match = _match_suffix(node.func)
            if match and match[1] == site.api:
                return node
    return None


def _module_constants(f: _File) -> dict[str, ast.Constant]:
    constants = {}
    for node in f.tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
    return constants


def _constructor_text(f: _File, binding: _Binding, helper: str, notes: list[str]) -> bytes:
    kept = []
    for keyword in binding.node.keywords:
        if keyword.arg is None or keyword.arg not in _PROVIDER_ONLY_OPTIONS:
            kept.append(f.source[slice(*f.span(keyword))])
        if keyword.arg is None:
            notes.append(f"{binding.ctor}() in {binding.file} takes options from `**{f.text(keyword.value)}`: "
                         "a base_url or api_key among them is ignored on Token Factory")
    if binding.node.args:
        notes.append(f"Dropped positional arguments to {binding.ctor}() in {binding.file}")
    return helper.encode() + b"(" + b", ".join(kept) + b")"


def _package_root(repo: _Repo, path: str) -> str | None:
    """The top-level package a file is imported as part of: the outermost of the folders above it that each have an
    __init__.py, without a gap (src/pkg/, or AutoGPT's autogpt_platform/backend/backend/ under a folder that has
    none). None for a file in a folder that isn't a package. The repo's own root never counts."""
    top = None
    folder = posixpath.dirname(path)
    while folder and (repo.root / folder / "__init__.py").exists():
        top, folder = folder, posixpath.dirname(folder)
    return top


def _vendor_dirs(repo: _Repo, files: set[str]) -> dict[str, str]:
    """Where each edited file gets the runtime: in its top-level package, imported relatively; or, for a file in a
    folder that isn't a package (a script's, like backend/main.py, or a namespace package's), beside it. At the repo
    root for a file there."""
    return {path: _package_root(repo, path) or posixpath.dirname(path) for path in files}


OPENAI_REQUIREMENT = "openai>=1.0"  # what the runtime imports; an app that only used Anthropic's SDK may not have it


def _requirement_names(lines: list[str]) -> set[str]:
    """The packages requirement lines name, normalized (`Foo_Bar` -> `foo-bar`). Options (`-r base.txt`) don't count."""
    names = set()
    for line in lines:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line.split(" #", 1)[0]) if not line.lstrip().startswith(("#", "-")) else None
        if match:
            names.add(re.sub(r"[-_.]+", "-", match[1]).lower())
    return names


def _with_openai(name: str, raw: bytes) -> bytes | None | bool:
    """A requirements.txt or pyproject.toml with `openai` added: True when it lists it already, None when it doesn't
    list `anthropic` either (so it may not be how the app installs) or OpenPort can't see where it would go."""
    text = raw.decode("utf-8", errors="replace")
    nl = newline_style(raw)
    if name == "requirements.txt":
        names = _requirement_names(text.splitlines())
        if "openai" in names:
            return True
        if "anthropic" not in names:
            return None
        return raw + (nl if raw and not raw.endswith(b"\n") else b"") + OPENAI_REQUIREMENT.encode() + nl
    try:  # pyproject.toml: PEP 621's [project] dependencies only
        dependencies = tomllib.loads(text).get("project", {}).get("dependencies")
    except tomllib.TOMLDecodeError:
        return None
    names = _requirement_names(dependencies) if isinstance(dependencies, list) and all(isinstance(d, str) for d in dependencies) else set()
    if "openai" in names:
        return True
    header = re.search(r"(?m)^\[project\][ \t]*(?:#[^\r\n]*)?\r?$", text)
    if "anthropic" not in names or header is None:
        return None
    following = re.search(r"(?m)^\[", text[header.end():])
    table = text[header.end(): header.end() + following.start() if following else len(text)]
    array = re.search(r"(?m)^dependencies[ \t]*=[ \t]*\[", table)
    if array is None:
        return None
    position = len(text[: header.end() + array.end()].encode())  # just inside the `[`: no trailing comma to mind
    after = text[header.end() + array.end():]
    if after.lstrip(" \t").startswith(("\n", "\r")):  # one per line: a line of its own, indented like the next
        indent = re.match(r"[ \t]*\r?\n([ \t]*)", after)[1] or "    "
        insert = nl + f'{indent}"{OPENAI_REQUIREMENT}",'.encode()
    else:
        insert = f'"{OPENAI_REQUIREMENT}", '.encode()
    return raw[:position] + insert + raw[position:]


def _openai_dependency(repo: _Repo, vendor_dirs: set[str]) -> tuple[dict[str, bytes], list[str]]:
    """The runtime imports `openai`: add it to the dependencies of each project the runtime lands in (the nearest
    folder up with a requirements.txt or pyproject.toml), where they list Anthropic's SDK and not OpenAI's. Returns
    those files, changed, and the runtimes whose project OpenPort couldn't do that for."""
    changed: dict[str, bytes] = {}
    unlisted = []
    for vendor_dir in sorted(vendor_dirs):
        folder = vendor_dir
        while folder and not any((repo.root / folder / name).is_file() for name in ("requirements.txt", "pyproject.toml")):
            folder = posixpath.dirname(folder)
        listed = False
        for name in ("requirements.txt", "pyproject.toml"):
            path = posixpath.join(folder, name)
            if path in changed:
                listed = True
            elif (repo.root / path).is_file():
                result = _with_openai(name, (repo.root / path).read_bytes())
                if isinstance(result, bytes):
                    changed[path] = result
                listed = listed or result is not None
        if not listed:
            unlisted.append(f"{vendor_dir}/{RUNTIME_MODULE}.py".lstrip("/"))
    return changed, unlisted


def _import_line(repo: _Repo, path: str, vendor_dir: str, helpers: set[str]) -> bytes:
    names = ", ".join(sorted(helpers))
    if not vendor_dir:  # a file at the root: the runtime beside it, on sys.path
        return f"from {RUNTIME_MODULE} import {names}".encode()
    if _package_root(repo, path) is None:
        # A folder with no __init__.py: imported as a namespace package (`import app.llm`), the runtime beside the
        # file is `.openport_llm`; run as a script (`python llm.py`), it's on sys.path, so `openport_llm`.
        return (f"if __package__:\n    from .{RUNTIME_MODULE} import {names}\n"
                f"else:\n    from {RUNTIME_MODULE} import {names}").encode()
    depth = len(PurePosixPath(path).parent.relative_to(vendor_dir).parts)
    return f"from {'.' * (depth + 1)}{RUNTIME_MODULE} import {names}".encode()


def _module_split(binding: "_Binding", ported: list[CallSiteFinding]) -> bool:
    """A module-level client, used by the calls that move only in its own file (`client.beta` counts: it's the client)."""
    return binding.top_level and binding.kind == "module" and all(
        s.file == binding.file and (s.receiver_kind == "module_global" or re.fullmatch(rf"{re.escape(binding.var)}(?:\.beta)+", s.receiver))
        for s in ported
    )


def _factory_split(f: "_File", binding: "_Binding", ported: list[CallSiteFinding]) -> bool | str:
    """A client built by a top-level factory that every call that moves gets by calling that factory by name
    (`get_client()` or `module.get_client()`). preflight's four agents share one `get_client()`: when one call has
    to stay on its provider, the others still move, on a second factory beside it. Not when the client is built
    from the factory's own parameters or locals, which the second factory wouldn't have: then why not."""
    name = binding.function
    if not name or "." in name or binding.kind not in ("module", "local", "return"):
        return False
    pattern = re.compile(rf"(?:[\w.]+\.)?{re.escape(name)}\(\)")
    if not all(s.receiver_kind == "factory" and pattern.fullmatch(s.receiver) for s in ported):
        return False
    factory = next((n for n in f.tree.body if isinstance(n, _FUNCTIONS) and n.name == name), None)
    if factory is None:
        return False
    args = factory.args
    declared = {n for node in ast.walk(factory) if isinstance(node, ast.Global) for n in node.names}
    own = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a}
    own |= {n.id for n in ast.walk(factory) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)} - declared
    kept = [k.value for k in binding.node.keywords if k.arg is None or k.arg not in _PROVIDER_ONLY_OPTIONS]
    read = sorted({n.id for value in kept for n in ast.walk(value) if isinstance(n, ast.Name) and n.id in own})
    if read:
        return (f"`{name}()` builds it from {', '.join(f'`{r}`' for r in read)}, which only `{name}()` has, so OpenPort "
                "can't build a second one beside it")
    return True


def _second_factory(f: "_File", name: str, new_name: str, constructor: bytes) -> list[Edit]:
    """`new_name()` beside the factory `name()`: the same client on Token Factory, built once."""
    factory = next(n for n in f.tree.body if isinstance(n, _FUNCTIONS) and n.name == name)
    nl = newline_style(f.source)
    end = line_end(f.source, f.span(factory)[1])
    block = (
        nl + nl + b"@functools.cache" + nl + f"def {new_name}():".encode() + nl
        + f'    """`{name}()`\'s client on NVIDIA Nemotron (Nebius Token Factory), for the calls OpenPort moved."""'.encode() + nl
        + b"    return " + constructor + nl
    )
    edits = [Edit(end, end, block)]
    imported = any(isinstance(n, ast.Import) and any(a.name == "functools" and not a.asname for a in n.names) for n in f.tree.body)
    if not imported:
        edits.append(_insert_import(f, b"import functools", used_at=end))  # the decorator runs as the module loads
    return edits


def _import_beside(f: "_File", name: str, new_name: str) -> Edit | None:
    """Import `new_name` from wherever this file imports `name` (keeping a relative import relative)."""
    for node in ast.walk(f.tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == name and not a.asname for a in node.names):
            module = "." * node.level + (node.module or "")
            nl = newline_style(f.source)
            end = line_end(f.source, f.span(node)[1])
            indent = indentation(f.source, f.span(node)[0])
            return Edit(end, end, indent + f"from {module} import {new_name}".encode() + nl)
    return None  # reached as `module.name()`, or defined in this file: nothing to import


def _insert_import(f: _File, statement: bytes, used_at: int | None = None) -> Edit:
    """`statement` on a line of its own after the file's imports, or, given where what it imports is first used, after
    the last import before that: a client built between two imports runs before the second one."""
    nl = newline_style(f.source)
    body = [n for n in f.tree.body if used_at is None or f.span(n)[1] <= used_at]
    imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports:
        position = line_end(f.source, f.span(imports[-1])[1])
    elif body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        position = line_end(f.source, f.span(body[0])[1])  # after the module docstring
    else:
        position = f.starts[0]  # after any byte order mark
    prefix = nl if position == len(f.source) and not f.source.endswith(b"\n") else b""
    return Edit(position, position, prefix + statement.replace(b"\n", nl) + nl)  # a statement may be a block of lines


def _cleanup_sdk_imports(f: _File, replaced: list[tuple[int, int]]) -> list[Edit]:
    """Drop SDK imports that nothing uses any more (type hints and exception handlers count as uses)."""

    def outside(node: ast.AST) -> bool:
        start, end = f.span(node)
        return not any(a <= start and end <= b for a, b in replaced)

    used = Counter(n.id for n in ast.walk(f.tree) if isinstance(n, ast.Name) and outside(n))
    edits = []
    for node in f.tree.body:
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] in SDK_MODULES:
            remaining = [a for a in node.names if used[a.asname or a.name]]
        elif isinstance(node, ast.Import) and all(a.name.split(".")[0] in SDK_MODULES for a in node.names):
            remaining = [a for a in node.names if used[a.asname or a.name.split(".")[0]]]
        else:
            continue
        if len(remaining) == len(node.names):
            continue
        start, end = f.span(node)
        if not remaining:
            edits.append(Edit(f.starts[node.lineno - 1], line_end(f.source, end), b""))
            continue
        names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in remaining)
        text = f"from {node.module} import {names}" if isinstance(node, ast.ImportFrom) else f"import {names}"
        edits.append(Edit(start, end, text.encode()))
    return edits


def _insert_last(f: _File, node: ast.AST, items: list[tuple[int, int]], text: str) -> list[Edit]:
    """Add `text` after the last of `items` (the spans of a call's arguments or a dict's entries), matching the layout."""
    if not items:
        return []
    last_start, last_end = max(items, key=lambda s: s[1])
    line_start = f.source.rfind(b"\n", 0, last_start) + 1
    if node.lineno == node.end_lineno or f.source[line_start:last_start].strip():
        return [Edit(last_end, last_end, b", " + text.encode())]
    # One per line: add a line with the same indentation, after any trailing comment.
    nl = newline_style(f.source)
    indent = f.source[line_start:last_start]
    after = f.source[last_end:before_newline(f.source, last_end)]
    edits = [] if after.lstrip().startswith(b",") else [Edit(last_end, last_end, b",")]
    end = before_newline(f.source, last_end)
    return edits + [Edit(end, end, nl + indent + text.encode() + b",")]


def _insert_kwarg(f: _File, call: ast.Call, text: str) -> list[Edit]:
    """Add `text` (e.g. "max_tokens=2000") as the call's last argument, matching its layout."""
    return _insert_last(f, call, [f.span(a) for a in [*call.args, *call.keywords]], text)


def _set_number(f: _File, current: ast.AST, value: float, floor: bool) -> list[Edit] | None:
    """Set a number written in the code (or raise it to `value`, if `floor`). None when it's an expression."""
    if not (isinstance(current, ast.Constant) and isinstance(current.value, (int, float)) and not isinstance(current.value, bool)):
        return None
    new = max(current.value, value) if floor else value
    return [] if new == current.value else [Edit(*f.span(current), repr(new).encode())]


def _set_number_kwarg(f: _File, call: ast.Call, names: tuple[str, ...], value: float, floor: bool) -> list[Edit] | None:
    """Set a numeric argument (or raise it to `value`, if `floor`). None when it's set by an expression, or isn't
    written out and the call passes `**kwargs`, which could set it too: passed twice, it'd be a TypeError."""
    keyword = next((k for k in call.keywords if k.arg in names), None)
    if keyword is None:
        return None if _spread(f, call) else _insert_kwarg(f, call, f"{names[0]}={value!r}") or None
    return _set_number(f, keyword.value, value, floor)


def _spread(f: _File, call: ast.Call) -> str | None:
    """The `**kwargs` a call passes, as written, if it passes one."""
    return next((f"**{f.text(k.value)}" for k in call.keywords if k.arg is None), None)


def _dict_value(node: ast.Dict, key: str) -> ast.AST | None:
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _entries(node: ast.Dict) -> dict[str, ast.AST]:
    return {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _entry_spans(f: _File, node: ast.Dict) -> list[tuple[int, int]]:
    return [(f.span(k)[0] if k is not None else f.span(v)[0] - 2, f.span(v)[1]) for k, v in zip(node.keys, node.values)]


def _key_quote(f: _File, node: ast.Dict) -> str:
    """The quote character the dict's keys are written with."""
    first = next((k for k in node.keys if isinstance(k, ast.Constant)), None)
    return "'" if first is not None and f.source[f.span(first)[0]:][:1] == b"'" else '"'


def _py_value(value, quote: str) -> str:
    """A JSON value as a Python literal, with the code's own quotes: {"enable_thinking": False}."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{quote}{k}{quote}: {_py_value(v, quote)}" for k, v in value.items()) + "}"
    return f"{quote}{value}{quote}" if isinstance(value, str) else repr(value)


def _set_number_entry(f: _File, node: ast.Dict, names: tuple[str, ...], value: float, floor: bool) -> list[Edit] | None:
    """Set a number in a request body written as a dict (or raise it to `value`, if `floor`)."""
    current = next((v for name in names if (v := _dict_value(node, name)) is not None), None)
    if current is None:
        quote = _key_quote(f, node)
        return _insert_last(f, node, _entry_spans(f, node), f"{quote}{names[0]}{quote}: {value!r}") or None
    return _set_number(f, current, value, floor)


def _add_entries(f: _File, node: ast.Dict, values: dict) -> list[Edit] | None:
    """Add Token Factory-only options (Nemotron's reasoning switch) to a request body. None if one is already set."""
    if any(_dict_value(node, key) is not None for key in values):
        return None
    quote, edits = _key_quote(f, node), []
    for key, value in values.items():
        added = _insert_last(f, node, _entry_spans(f, node), f"{quote}{key}{quote}: {_py_value(value, quote)}")
        if not added:
            return None
        edits += added
    return edits


def _assigned_value(scope: ast.AST, name: str, before_line: int) -> ast.AST | None:
    """The value most recently assigned to `name` in `scope` before a line."""
    best = None
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and node.lineno < before_line:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and (best is None or node.lineno > best.lineno):
                best = node
    return best.value if best else None


def _module_value(f: _File | None, name: str) -> ast.AST | None:
    """The value of a module-level assignment (the last one wins); function bodies don't count."""
    value = None
    for node in f.tree.body if f else []:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                value = node.value
    return value


def _system_expression(f: _File, arguments: dict[str, ast.AST], sdk: str, function: ast.AST | None, line: int) -> ast.AST | None:
    """The expression a call uses as its system prompt, from its arguments (or a plain HTTP call's request body)."""
    if sdk == "anthropic":
        system = arguments.get("system")
        if isinstance(system, ast.List) and system.elts and isinstance(system.elts[-1], ast.Dict):
            return _dict_value(system.elts[-1], "text")
        return system
    messages = arguments.get("messages")
    if isinstance(messages, ast.Name):
        messages = (_assigned_value(function, messages.id, line) if function else None) or _module_value(f, messages.id)
    if isinstance(messages, ast.List) and messages.elts and isinstance(messages.elts[0], ast.Dict):
        first = messages.elts[0]
        role = _dict_value(first, "role")
        if isinstance(role, ast.Constant) and role.value in ("system", "developer"):
            return _dict_value(first, "content")
    return None


def _resolve_string(repo: "_Repo", f: _File, node: ast.AST, function: ast.AST | None, line: int) -> tuple[_File, ast.AST, bool] | None:
    """Follow a prompt expression back to the string literal it's built from: (file, literal, is a template)."""
    template = False
    for _ in range(6):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return f, node, template
        if isinstance(node, ast.JoinedStr):
            return f, node, True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "strip", "rstrip"):
            template = template or node.func.attr == "format"
            node = node.func.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            node = node.right  # PROMPT + EXTRA: append to the last part
        elif isinstance(node, ast.Name):
            value = (_assigned_value(function, node.id, line) if function else None) or _module_value(f, node.id)
            if value is None:  # imported from another module of the app
                module, original = repo.imports(f)[0].get(node.id, ("", ""))
                f = repo.file(repo.module_path(module, f.path))
                value = _module_value(f, original) if f else None
            if value is None:
                return None
            node, function, line = value, None, 10**9
        else:
            return None
    return None


def _prompt_literal(repo: "_Repo", f: _File, call: ast.Call, sdk: str,
                    arguments: dict[str, ast.AST] | None = None) -> tuple[_File, ast.AST, bool] | None:
    """The string literal a call's system prompt is written in (file, literal, is a template), when OpenPort can see it."""
    function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None)
    arguments = arguments if arguments is not None else {k.arg: k.value for k in call.keywords if k.arg}
    expression = _system_expression(f, arguments, sdk, function, call.lineno)
    return _resolve_string(repo, f, expression, function, call.lineno) if expression is not None else None


def _kept_prompts(repo: "_Repo", sites: list[CallSiteFinding], plan: dict[str, SiteDecision], http_calls: dict) -> set:
    """The prompt literals calls that stay use, as (file, "line:column"): an addendum tuned for a call that moves
    isn't written into those."""
    found = set()
    for site in sites:
        f = repo.file(site.file)
        if f is None or not plan[site.id].keep:
            continue
        if site.sdk == "http":
            http = http_calls.get(site.id)
            payload = _payload(f, http) if http else None
            resolved = _prompt_literal(repo, f, http.call, "http", _entries(payload)) if payload is not None else None
        else:
            call = _call_node(f, site)
            resolved = _prompt_literal(repo, f, call, site.sdk) if call is not None else None
        if resolved:
            target, literal, _ = resolved
            found.add((target.path, f"{literal.lineno}:{literal.col_offset}"))
    return found


def _apply_addendum(repo: "_Repo", f: _File, call: ast.Call, site: CallSiteFinding, decision: SiteDecision, edits: dict,
                    text_edits: dict, applied: set, kept_prompts: set, arguments: dict[str, ast.AST] | None = None) -> str | None:
    """Write the tuned addendum into each system prompt the call uses. What to do by hand if any couldn't be placed."""
    addendum = decision.system_addendum
    resolved = _prompt_literal(repo, f, call, site.sdk, arguments)
    if resolved:
        target, literal, template = resolved
        if (target.path, f"{literal.lineno}:{literal.col_offset}") in kept_prompts:
            return f"Tuned: a call that stays uses this system prompt too, so give this call its own, with \"{addendum}\" appended"
        key = (target.path, f"{literal.lineno}:{literal.col_offset}", addendum)
        if key in applied:
            return None
        edit = python_literal_insertion(target.source, *target.span(literal), addendum, template)
        if edit:
            edits[target.path].append(edit)
            applied.add(key)
            return None
    # Prompts loaded from files: find each prompt the app actually sent.
    placed = bool(decision.system_prompts)
    for prompt in decision.system_prompts:
        hit = find_prompt_file(repo.root, prompt)
        if hit is None:
            placed = False
            continue
        path, offset, raw = hit
        if (path, offset, addendum) not in applied:
            text_edits.setdefault(path, (raw, []))[1].append(prompt_file_insertion(raw, offset, addendum))
            applied.add((path, offset, addendum))
    return None if placed else f"Tuned: append to the system prompt by hand: \"{addendum}\""


def _anthropic_error_names(f: _File) -> list[str]:
    """Anthropic SDK exception classes this file refers to (they won't be raised by ported calls)."""
    aliases = {a.asname or a.name for n in ast.walk(f.tree) if isinstance(n, ast.Import) for a in n.names if a.name == "anthropic"}
    imported = {
        a.asname or a.name
        for n in ast.walk(f.tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "anthropic"
        for a in n.names
    }
    found = set()
    for node in ast.walk(f.tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases and node.attr.endswith(("Error", "Exception")):
            found.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in imported and node.id.endswith(("Error", "Exception")):
            found.add(node.id)
    return sorted(found)


# --- plain HTTP calls -------------------------------------------------------------------------------


def _reaching(repo: _Repo, f: _File, node: ast.AST, function: ast.AST | None, line: int,
              depth: int = 3) -> list[tuple[_File, ast.AST, ast.AST | None]]:
    """An expression and the values it's built from: each name followed to its assignment in the function, the
    module, or the app module it's imported from. (file, value, the function it's assigned in) triples."""
    found = [(f, node, function)]
    if depth == 0:
        return found
    for name in sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}):
        target, scope = f, function
        value = _assigned_value(function, name, line) if function else None
        if value is None:
            scope, value = None, _module_value(f, name)
        if value is None:
            module, original = repo.imports(f)[0].get(name, ("", ""))
            target = repo.file(repo.module_path(module, f.path))
            value = _module_value(target, original) if target else None
        if value is not None:
            found += _reaching(repo, target, value, scope, value.lineno if scope else 10**9, depth - 1)
    return found


def _string_literals(node: ast.AST) -> list[ast.AST]:
    """The string literals in an expression; an f-string counts as one."""
    if isinstance(node, ast.JoinedStr) or isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    return [literal for child in ast.iter_child_nodes(node) for literal in _string_literals(child)]


def _env_names(node: ast.AST) -> list[ast.Constant]:
    """The names of the environment variables an expression reads: os.environ["X"], os.getenv("X"), environ.get("X")."""
    names = []
    for n in ast.walk(node):
        key = None
        if isinstance(n, ast.Subscript) and ast.unparse(n.value).endswith("environ"):
            key = n.slice
        elif isinstance(n, ast.Call) and n.args and ast.unparse(n.func).endswith(("getenv", "environ.get")):
            key = n.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.append(key)
    return names


@dataclass
class _HttpCall:
    """A plain HTTP call, and what it's built from."""

    call: ast.Call
    function: ast.AST | None
    url: ast.AST
    kwargs: dict[str, ast.AST]
    url_literals: list[tuple[_File, ast.AST]]  # the strings its URL is made of
    url_variables: list[tuple[_File, ast.Constant]]  # the OpenAI SDK's base URL variables it reads
    authorization: ast.AST | None  # its Authorization header's value, when OpenPort can see it
    key_variables: list[tuple[_File, ast.Constant]]  # the environment variables that header reads


def _unique(pairs: list[tuple[_File, ast.AST]]) -> list[tuple[_File, ast.AST]]:
    return list({(f.path, f.span(node)): (f, node) for f, node in pairs}.values())


def _http_call(repo: _Repo, f: _File, site: CallSiteFinding) -> _HttpCall | None:
    call = next((n for n in ast.walk(f.tree) if isinstance(n, ast.Call) and n.lineno == site.line
                 and isinstance(n.func, ast.Attribute) and n.func.attr in HTTP_METHODS), None)
    if call is None:
        return None
    function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None)
    kwargs = {k.arg: k.value for k in call.keywords if k.arg}
    position = HTTP_METHODS[call.func.attr]
    url = kwargs.get("url") or (call.args[position] if len(call.args) > position else None)
    if url is None:
        return None
    reached = _reaching(repo, f, url, function, call.lineno)
    literals = _unique([(target, literal) for target, node, _ in reached for literal in _string_literals(node)])
    variables = _unique([(target, name) for target, node, _ in reached for name in _env_names(node) if name.value in BASE_URL_VARIABLES])
    authorization, keys = None, []
    headers = kwargs.get("headers")
    for target, node, scope in _reaching(repo, f, headers, function, call.lineno, depth=1) if headers is not None else []:
        if isinstance(node, ast.Dict):
            authorization = next((v for k, v in zip(node.keys, node.values)
                                  if isinstance(k, ast.Constant) and str(k.value).lower() == "authorization"), None)
            if authorization is not None:
                line = authorization.lineno
                keys = _unique([(t, name) for t, n, _ in _reaching(repo, target, authorization, scope, line) for name in _env_names(n)])
                break
    return _HttpCall(call, function, url, kwargs, literals, variables, authorization, keys)


def _payload(f: _File, http: _HttpCall) -> ast.Dict | None:
    """The request body, when the code writes it as a dict: json={...}, or data=json.dumps({...})."""
    payload = http.kwargs.get("json")
    body = http.kwargs.get("data") or http.kwargs.get("content")
    if payload is None and isinstance(body, ast.Call) and body.args and ast.unparse(body.func).endswith("dumps"):
        payload = body.args[0]
    if isinstance(payload, ast.Name):
        payload = (_assigned_value(http.function, payload.id, http.call.lineno) if http.function else None) or _module_value(f, payload.id)
    return payload if isinstance(payload, ast.Dict) else None


def _split_url(f: _File, url: ast.AST) -> list[Edit]:
    """Token Factory's URL for a call whose URL also serves calls that stay: a second constant beside the module
    constant it names (CHAT_URL_NEMOTRON), as a shared client is split, or else the URL itself at the call."""
    statement = None
    for node in f.tree.body if isinstance(url, ast.Name) else []:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == url.id for t in targets):
                statement = node
    if statement is None:
        return [Edit(*f.span(url), f'"{CHAT_URL}"'.encode())]
    name = url.id + ("_NEMOTRON" if url.id.isupper() else "_nemotron")
    position = line_end(f.source, f.span(statement)[1])
    nl = newline_style(f.source)
    prefix = nl if position == len(f.source) and not f.source.endswith(b"\n") else b""
    return [Edit(position, position, prefix + f'{name} = "{CHAT_URL}"'.encode() + nl), Edit(*f.span(url), name.encode())]


def _http_calls(repo: _Repo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision]) -> dict[str, _HttpCall | None]:
    """Each plain HTTP call, and which can move at all: one whose URL OpenPort can't see stays where it is."""
    calls = {site.id: _http_call(repo, f, site) if (f := repo.file(site.file)) else None for site in sites}
    for site in sites:
        decision, http = plan[site.id], calls[site.id]
        if decision.keep:
            continue
        if http is None:
            decision.keep_because("couldn't locate the call in the source")
            continue
        texts = [f.source[slice(*f.span(node))].decode("utf-8", errors="replace") for f, node in http.url_literals]
        reason = why_kept(texts, ast.unparse(http.url))
        if reason:
            decision.keep_because(reason)
    return calls


def _port_http(repo: _Repo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], calls: dict[str, _HttpCall | None],
               edits: dict, const_uses: dict, text_edits: dict, applied: set, kept_prompts: set) -> None:
    """Point each ported plain HTTP call at Token Factory: its URL, its key and its model, where the code spells
    them out (`calls` from _http_calls)."""

    def key(f: _File, node: ast.AST) -> tuple[str, tuple[int, int]]:
        return f.path, f.span(node)

    # Strings and variables calls that stay also use: those aren't edited.
    shared = {key(f, node) for s in sites if plan[s.id].keep and calls[s.id]
              for f, node in [*calls[s.id].url_literals, *calls[s.id].url_variables, *calls[s.id].key_variables]}

    for site in sites:
        f, decision, http = repo.file(site.file), plan[site.id], calls[site.id]
        payload = _payload(f, http) if http else None
        model = _dict_value(payload, "model") if payload is not None else None
        constants = _module_constants(f) if f else {}
        if isinstance(model, ast.Name) and model.id in constants:
            const_uses[(f.path, model.id)].append((decision.model, model))  # kept calls count too (model None)
        if decision.keep:
            continue

        # The URL: the strings it's built from, unless calls that stay use them too.
        texts = [t.source[slice(*t.span(node))].decode("utf-8", errors="replace") for t, node in http.url_literals]
        changes = url_edits(texts)
        if any(key(*http.url_literals[i]) in shared for i in changes):
            edits[f.path] += _split_url(f, http.url)
        else:
            for i, text in changes.items():
                target, node = http.url_literals[i]
                renamed = re.sub(rf"(['\"])({'|'.join(BASE_URL_VARIABLES)})\1", rf"\1{BASE_URL_VARIABLE}\1", text)
                edits[target.path].append(Edit(*target.span(node), renamed.encode()))
            changed = [(t.path, *t.span(n)) for i, (t, n) in enumerate(http.url_literals) if i in changes]
            for target, name in http.url_variables:
                start, end = target.span(name)
                if key(target, name) not in shared and not any(p == target.path and a <= start and end <= b for p, a, b in changed):
                    edits[target.path].append(Edit(start, end, quoted_like(target.source[start:end], BASE_URL_VARIABLE)))

        # The key, where it's read from the environment.
        for target, name in http.key_variables:
            if key(target, name) in shared:
                decision.notes.append(f"Its key ({name.value}) is read for calls that stay too: send {KEY_VARIABLE} here by hand")
            elif name.value != KEY_VARIABLE:
                edits[target.path].append(Edit(*target.span(name), quoted_like(target.source[slice(*target.span(name))], KEY_VARIABLE)))
        if http.authorization is None:
            decision.notes.append(f"Send {KEY_VARIABLE} as its bearer token: OpenPort couldn't find its Authorization header")
        elif not http.key_variables:
            written = all(isinstance(n, (ast.Constant, ast.JoinedStr, ast.FormattedValue)) for n in ast.walk(http.authorization)
                          if isinstance(n, ast.expr))
            source = "is written in the code" if written else f"comes from `{ast.unparse(http.authorization)}`"
            decision.notes.append(f"Its key {source}: send {KEY_VARIABLE} instead")

        # The model and tuned settings, in the request body.
        if payload is None:
            decision.notes.append(f"OpenPort couldn't see its request body: make sure it sends model {decision.model}")
            continue
        if model is None:
            decision.notes.append(f"No `model` in its request body: make sure {decision.model} is sent")
        elif isinstance(model, ast.Constant) and isinstance(model.value, str):
            edits[f.path].append(Edit(*f.span(model), quoted_like(f.source[slice(*f.span(model))], decision.model)))
            comment = was_comment(f.source, f.span(model)[1], model.value, b"#")
            if comment:
                edits[f.path].append(comment)
        elif not (isinstance(model, ast.Name) and model.id in constants):
            decision.notes.append(f"Model comes from `{f.text(model)}`: set it to {decision.model}")
        if decision.extra_body:
            added = _add_entries(f, payload, decision.extra_body)
            if added is None:
                decision.notes.append(f"Send {decision.extra_body!r} in its request body by hand")
            else:
                edits[f.path] += added
        for names, number, floor, label in (
            (("max_tokens", "max_completion_tokens"), decision.max_tokens, True, "allow at least {} output tokens"),
            (("temperature",), decision.temperature, False, "temperature {}"),
        ):
            if number is None or (floor and not number):
                continue
            changed = _set_number_entry(f, payload, names, number, floor)
            if changed is None:
                decision.notes.append(f"Tuned: {label.format(number)} (set by hand)")
            else:
                edits[f.path] += changed
        if decision.system_addendum and (note := _apply_addendum(repo, f, http.call, site, decision, edits, text_edits, applied,
                                                                  kept_prompts, _entries(payload))):
            decision.notes.append(note)


def port_python(
    root: Path, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], runtime_source: bytes
) -> dict[str, bytes]:
    """Rewrite the Python files under `root` for the ported sites in `plan`. Returns changed and added files.

    Updates `plan` in place: sites that can't be ported mechanically are kept, with the reason.
    """
    repo = _Repo(root)
    http_sites = [site for site in sites if site.sdk == "http"]
    sites = [site for site in sites if site.sdk != "http"]
    links = {site.id: repo.link(site) for site in sites}
    partly: dict[str, list[_Binding]] = {}  # handed-in clients traced through only some of the callers
    for site in sites:
        if not links[site.id] and not plan[site.id].keep:
            found, complete = repo.handed_in(site)
            (links if complete else partly)[site.id] = found
    for site in sites:  # where the code picks no single provider, rewriting the client would override a choice made elsewhere
        urls = _providers(links[site.id])
        chosen = next((how for b in links[site.id] if (how := _chosen_by_caller(repo.file(b.file), b))), None)
        if plan[site.id].keep:
            continue
        if not links[site.id] and (parameter := _parameter_client(repo.file(site.file), site)):
            plan[site.id].keep_because(f"its client is whatever its caller passes (the `{parameter}` parameter): port the code that "
                                       "builds that client instead")
        elif not links[site.id] and (why := _foreign_client(repo.file(site.file), site)):
            plan[site.id].keep_because(why)
        if len(urls) > 1:
            plan[site.id].keep_because(f"its client is one of several, for different providers (base URLs {', '.join(urls)}): "
                                       "add Token Factory among them by hand")
        elif chosen:
            plan[site.id].keep_because(f"its client takes its provider from whoever builds it ({chosen}): point that at "
                                       f"{TOKEN_FACTORY_V1} with {KEY_VARIABLE} instead")
    sites_by_id = {site.id: site for site in sites}
    for site_id, found in partly.items():  # it stays, and still uses the clients those callers hand it: none is swapped under it
        if not plan[site_id].keep:
            plan[site_id].keep_because(f"couldn't find where its client (`{sites_by_id[site_id].receiver}`) is created")
        links[site_id] = found

    def key(binding: _Binding) -> tuple:
        return binding.file, binding.node.lineno, binding.node.col_offset

    linked = {(site.file, site.line, key(b)) for site in sites for b in links[site.id]}
    other_uses: dict[tuple, list[str]] = {}

    def held(binding: _Binding) -> list[str]:  # what else the client does: it stays on the original provider for that
        if key(binding) not in other_uses:
            other_uses[key(binding)] = _other_uses(repo, binding, lambda path, line: (path, line, key(binding)) in linked)
        return other_uses[key(binding)]

    actions = decide_client_actions(
        sites,
        plan,
        links,
        key=key,
        # A client can be split (a second one on Token Factory beside it, for the calls that move) when it's built
        # at module level and used in its own file, or built by a top-level factory every moving call calls by name.
        splittable=lambda b, ported: _module_split(b, ported) or _factory_split(repo.file(b.file), b, ported),
        other_uses=held,
    )

    edits: dict[str, list[Edit]] = defaultdict(list)
    helpers: dict[str, set[str]] = defaultdict(set)
    first_use: dict[str, int] = {}  # where each file first uses a helper: its import goes above that
    replaced: dict[str, list[tuple[int, int]]] = defaultdict(list)
    split_names: dict[str, str] = {}  # site id -> new client variable, or new factory name
    factory_imports: set[tuple[str, str, str]] = set()  # (site file, factory name, new factory name)

    for action in actions.values():
        binding: _Binding = action.binding
        f = repo.file(binding.file)
        helper = HELPERS[binding.ctor]
        notes = plan[action.ported[0].id].notes
        text = _constructor_text(f, binding, helper, notes)
        helpers[f.path].add(helper)
        if action.action == "replace":
            span = f.span(binding.node)
            edits[f.path].append(Edit(*span, text))
            replaced[f.path].append(span)
            used_at = span[0]
        elif _module_split(binding, action.ported):  # a second client next to the original, for the ported calls only
            new_var = f"{binding.var}_nemotron"
            statement_end = line_end(f.source, f.span(binding.statement)[1])
            indent = indentation(f.source, f.span(binding.statement)[0])
            line = indent + f"{new_var} = ".encode() + text + newline_style(f.source)
            edits[f.path].append(Edit(statement_end, statement_end, line))
            used_at = statement_end
            for site in action.ported:
                split_names[site.id] = new_var
        else:  # a second factory next to the original, for the ported calls only
            new_name = f"{binding.function}_nemotron"
            edits[f.path] += _second_factory(f, binding.function, new_name, text)
            used_at = line_end(f.source, f.span(next(n for n in f.tree.body if isinstance(n, _FUNCTIONS) and n.name == binding.function))[1])
            for site in action.ported:
                split_names[site.id] = new_name
                if site.file != binding.file:
                    factory_imports.add((site.file, binding.function, new_name))
        first_use[f.path] = min(first_use.get(f.path, used_at), used_at)

    # Models, tuned settings and receivers at each ported call site.
    const_uses: dict[tuple[str, str], list[tuple[str | None, ast.Name]]] = defaultdict(list)
    text_edits: dict[str, tuple[bytes, list[Edit]]] = {}  # prompt files: path -> (original bytes, edits)
    applied: set = set()  # prompt insertions already made, so a shared prompt gets the addendum once
    http_calls = _http_calls(repo, http_sites, plan)
    kept_prompts = _kept_prompts(repo, [*sites, *http_sites], plan, http_calls)
    for site in sites:
        f = repo.file(site.file)
        decision = plan[site.id]
        call = _call_node(f, site) if f else None
        if call is None:
            if not decision.keep:
                decision.keep_because("couldn't locate the call in the source")
            continue
        model_kw = next((k for k in call.keywords if k.arg == "model"), None)
        value = model_kw.value if model_kw else None
        constants = _module_constants(f)
        if isinstance(value, ast.Name) and value.id in constants:
            const_uses[(f.path, value.id)].append((decision.model, value))  # kept calls count too (model None)
        if decision.keep:
            continue

        if value is None:
            decision.notes.append(f"No `model=` argument here: make sure {decision.model} is passed")
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            edits[f.path].append(Edit(*f.span(value), quoted_like(f.source[slice(*f.span(value))], decision.model)))
            comment = was_comment(f.source, f.span(value)[1], value.value, b"#")
            if comment:
                edits[f.path].append(comment)
        elif not (isinstance(value, ast.Name) and value.id in constants):
            decision.notes.append(f"Model comes from `{f.text(value)}`: set it to {decision.model}")

        spread = _spread(f, call)

        def by_hand(*names: str) -> str:  # a setting the call doesn't write out, beside a `**kwargs` that could set it too
            written = any(k.arg in names for k in call.keywords)
            return f" (set by hand: the call passes `{spread}`, which may set it too)" if spread and not written else " (set by hand)"

        if decision.extra_body:
            inserted = [] if any(k.arg == "extra_body" for k in call.keywords) or spread else \
                _insert_kwarg(f, call, f"extra_body={decision.extra_body!r}")
            if inserted:
                edits[f.path] += inserted
            else:
                decision.notes.append(f"Pass extra_body={decision.extra_body!r} here{by_hand('extra_body')}")
        if decision.max_tokens:
            changed = _set_number_kwarg(f, call, ("max_tokens", "max_completion_tokens"), decision.max_tokens, floor=True)
            if changed is None:
                decision.notes.append(f"Tuned: allow at least {decision.max_tokens} output tokens"
                                      f"{by_hand('max_tokens', 'max_completion_tokens')}")
            else:
                edits[f.path] += changed
        if decision.temperature is not None:
            changed = _set_number_kwarg(f, call, ("temperature",), decision.temperature, floor=False)
            if changed is None:
                decision.notes.append(f"Tuned: temperature {decision.temperature}{by_hand('temperature')}")
            else:
                edits[f.path] += changed
        if decision.system_addendum and (note := _apply_addendum(repo, f, call, site, decision, edits, text_edits, applied, kept_prompts)):
            decision.notes.append(note)

        if site.id in split_names:
            base = call.func
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Call):  # a factory: call the second one instead (`get_client()` -> `get_client_nemotron()`)
                callee = base.func
                name_node = callee if isinstance(callee, ast.Name) else None
                if isinstance(callee, ast.Attribute):  # `clients.get_client()`: rename the attribute only
                    start = f.span(callee)[1] - len(callee.attr.encode())
                    edits[f.path].append(Edit(start, f.span(callee)[1], split_names[site.id].encode()))
                elif name_node is not None:
                    edits[f.path].append(Edit(*f.span(name_node), split_names[site.id].encode()))
            else:
                edits[f.path].append(Edit(*f.span(base), split_names[site.id].encode()))
    for path, factory, new_name in sorted(factory_imports):
        edit = _import_beside(repo.file(path), factory, new_name)
        if edit is not None:
            edits[path].append(edit)
    _port_http(repo, http_sites, plan, http_calls, edits, const_uses, text_edits, applied, kept_prompts)

    # Shared model constants: edit the constant once if every call using it moves to the same model and nothing
    # else reads it (another module, a tokenizer); otherwise write the model at each ported call, and leave the
    # constant as it was for the rest.
    for (path, name), uses in const_uses.items():
        f = repo.file(path)
        targets = {model for model, _ in uses}
        constant = _module_constants(f)[name]
        if len(targets) == 1 and None not in targets and _read_only_at(repo, f, name, [node for _, node in uses]):
            (target,) = targets
            edits[path].append(Edit(*f.span(constant), quoted_like(f.source[slice(*f.span(constant))], target)))
            comment = was_comment(f.source, f.span(constant)[1], constant.value, b"#")
            if comment:
                edits[path].append(comment)
        else:
            for model, node in uses:
                if model is not None:
                    edits[path].append(Edit(*f.span(node), f'"{model}"'.encode()))

    # Imports, and a warning wherever ported calls are wrapped in Anthropic exception handlers.
    changed_files = set(edits) | set(helpers)
    vendor_dirs = _vendor_dirs(repo, set(helpers))
    for path, used_helpers in helpers.items():
        f = repo.file(path)
        edits[path].append(_insert_import(f, _import_line(repo, path, vendor_dirs[path], used_helpers), first_use.get(path)))
        edits[path] += _cleanup_sdk_imports(f, replaced[path])
    anthropic = any(helper.endswith("anthropic_client") for used in helpers.values() for helper in used)
    dependencies, unlisted = _openai_dependency(repo, set(vendor_dirs.values())) if anthropic else ({}, [])
    for site in sites:
        f = repo.file(site.file)
        if f and site.sdk == "anthropic" and not plan[site.id].keep:
            errors = _anthropic_error_names(f)
            if errors:
                plan[site.id].notes.append(
                    f"{site.file} handles {', '.join(errors)}: ported calls raise openai errors instead"
                )
            if unlisted:
                plan[site.id].notes.append(f"OpenPort's runtime ({', '.join(unlisted)}) imports the `openai` package: add it to the "
                                           "app's dependencies (OpenPort adds it to a requirements.txt or pyproject.toml that "
                                           "lists `anthropic`)")

    changed_files |= set(edits)
    # Calls sharing a string each edit it, and settings added after the same last argument each add its comma: keep one.
    output = {path: apply_edits(repo.file(path).source, list(dict.fromkeys(edits[path]))) for path in changed_files if edits[path]}
    output |= {path: apply_edits(raw, file_edits) for path, (raw, file_edits) in text_edits.items()}
    output |= dependencies
    for vendor_dir in sorted(set(vendor_dirs.values())):
        output[f"{vendor_dir}/{RUNTIME_MODULE}.py".lstrip("/")] = runtime_source
    return output
