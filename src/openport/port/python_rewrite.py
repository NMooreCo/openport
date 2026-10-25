"""Rewrite Python code onto the OpenPort runtime: swap SDK clients, and point each ported call at its Nemotron model.
Plain HTTP calls (requests, httpx, aiohttp) move to Token Factory's URL and key instead (see http.py)."""

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..runtime.openport_llm import _PROVIDER_ONLY_OPTIONS
from ..scan import _source_files
from ..scan.findings import CallSiteFinding
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
        return cls(path, source, tree, line_starts(source), parents)

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
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is node:
            target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
            if isinstance(target, ast.Name):
                var = target.id
                declared_global = bool(functions) and any(
                    isinstance(s, ast.Global) and var in s.names for s in ast.walk(functions[0])
                )
                kind = "module" if not functions or declared_global else "local"
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id in ("self", "cls"):
                kind, var = "attribute", target.attr
        elif isinstance(statement, ast.Return) and statement.value is node:
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
        if kind == "module_global":
            return [b for b in self.bindings(f) if b.kind == "module" and b.var == receiver]
        if kind == "local":
            return [b for b in self.bindings(f) if b.kind == "local" and b.var == receiver and b.function == site.function]
        if kind == "attribute":
            match = re.fullmatch(r"(?:self|cls)\.(\w+)", receiver)
            cls = site.function.split(".")[-2] if "." in site.function else None
            return [b for b in self.bindings(f) if match and b.kind == "attribute" and b.var == match[1] and b.cls == cls]
        if kind == "imported":
            names, _ = self.imports(f)
            module, original = names.get(receiver, ("", ""))
            target = self.file(self.module_path(module, f.path))
            return [b for b in self.bindings(target) if b.kind == "module" and b.var == original] if target else []
        if kind == "factory":
            match = re.fullmatch(r"([\w.]+)\(.*\)", receiver, re.S)
            return self.made_by(f, match[1], site.function) if match else []
        if kind == "other" and re.fullmatch(r"\w+", receiver):  # `client = get_client(...)` earlier in the function
            call = _call_node(f, site)
            function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None) if call else None
            value = _assigned_value(function, receiver, site.line) if function else None
            value = value.value if isinstance(value, ast.Await) else value
            if isinstance(value, ast.Call) and not _constructor_sdk(value) and re.fullmatch(r"[\w.]+", ast.unparse(value.func)):
                return self.made_by(f, ast.unparse(value.func), site.function)
        return []

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


def _parameter_client(f: _File | None, site: CallSiteFinding) -> str | None:
    """The parameter a call's client arrives through, when it's a parameter of the function the call is in."""
    receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
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
    if binding.node.args:
        notes.append(f"Dropped positional arguments to {binding.ctor}() in {binding.file}")
    return helper.encode() + b"(" + b", ".join(kept) + b")"


def _vendor_dir(repo: _Repo, files: set[str]) -> str:
    """Put the runtime in the top-level package all edited files share, or at the repo root."""
    tops = set()
    for path in files:
        parts = path.split("/")
        top = "/".join(parts[:2]) if parts[0] == "src" and len(parts) > 2 else parts[0] if len(parts) > 1 else ""
        tops.add(top if top and (repo.root / top / "__init__.py").exists() else "")
    return tops.pop() if len(tops) == 1 else ""


def _import_line(path: str, vendor_dir: str, helpers: set[str]) -> bytes:
    names = ", ".join(sorted(helpers))
    if not vendor_dir:
        return f"from {RUNTIME_MODULE} import {names}".encode()
    depth = len(PurePosixPath(path).parent.relative_to(vendor_dir).parts)
    return f"from {'.' * (depth + 1)}{RUNTIME_MODULE} import {names}".encode()


def _insert_import(f: _File, statement: bytes) -> Edit:
    nl = newline_style(f.source)
    body = f.tree.body
    imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports:
        position = line_end(f.source, f.span(imports[-1])[1])
    elif body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        position = line_end(f.source, f.span(body[0])[1])  # after the module docstring
    else:
        position = 0
    prefix = nl if position == len(f.source) and not f.source.endswith(b"\n") else b""
    return Edit(position, position, prefix + statement + nl)


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
    """Set a numeric argument (or raise it to `value`, if `floor`). None when it's set by an expression."""
    keyword = next((k for k in call.keywords if k.arg in names), None)
    if keyword is None:
        return _insert_kwarg(f, call, f"{names[0]}={value!r}") or None
    return _set_number(f, keyword.value, value, floor)


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


def _apply_addendum(repo: "_Repo", f: _File, call: ast.Call, site: CallSiteFinding, decision: SiteDecision,
                    edits: dict, text_edits: dict, applied: set, arguments: dict[str, ast.AST] | None = None) -> bool:
    """Write the tuned addendum into each system prompt the call uses. False if any couldn't be placed."""
    addendum = decision.system_addendum
    function = next((a for a in f.ancestors(call) if isinstance(a, _FUNCTIONS)), None)
    arguments = arguments if arguments is not None else {k.arg: k.value for k in call.keywords if k.arg}
    expression = _system_expression(f, arguments, site.sdk, function, call.lineno)
    resolved = _resolve_string(repo, f, expression, function, call.lineno) if expression is not None else None
    if resolved:
        target, literal, template = resolved
        key = (target.path, f"{literal.lineno}:{literal.col_offset}", addendum)
        if key in applied:
            return True
        edit = python_literal_insertion(target.source, *target.span(literal), addendum, template)
        if edit:
            edits[target.path].append(edit)
            applied.add(key)
            return True
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
    return placed


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


def _port_http(repo: _Repo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], edits: dict, const_uses: dict,
               text_edits: dict, applied: set) -> None:
    """Point each ported plain HTTP call at Token Factory: its URL, its key and its model, where the code spells
    them out. A call whose URL OpenPort can't see stays where it is."""
    calls = {site.id: _http_call(repo, f, site) if (f := repo.file(site.file)) else None for site in sites}

    def key(f: _File, node: ast.AST) -> tuple[str, tuple[int, int]]:
        return f.path, f.span(node)

    def kept_uses() -> set:  # strings and variables calls that stay also use: those aren't edited
        return {key(f, node) for s in sites if plan[s.id].keep and calls[s.id]
                for f, node in [*calls[s.id].url_literals, *calls[s.id].url_variables, *calls[s.id].key_variables]}

    for site in sites:  # first, which calls can move at all
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
    shared = kept_uses()

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
        if decision.system_addendum and not _apply_addendum(repo, f, http.call, site, decision, edits, text_edits, applied, _entries(payload)):
            decision.notes.append(f"Tuned: append to the system prompt by hand: \"{decision.system_addendum}\"")


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
    for site in sites:  # where the code picks no single provider, rewriting the client would override a choice made elsewhere
        urls = _providers(links[site.id])
        chosen = next((how for b in links[site.id] if (how := _chosen_by_caller(repo.file(b.file), b))), None)
        if plan[site.id].keep:
            continue
        if not links[site.id] and (parameter := _parameter_client(repo.file(site.file), site)):
            plan[site.id].keep_because(f"its client is whatever its caller passes (the `{parameter}` parameter): port the code that "
                                       "builds that client instead")
        if len(urls) > 1:
            plan[site.id].keep_because(f"its client is one of several, for different providers (base URLs {', '.join(urls)}): "
                                       "add Token Factory among them by hand")
        elif chosen:
            plan[site.id].keep_because(f"its client takes its provider from whoever builds it ({chosen}): point that at "
                                       f"{TOKEN_FACTORY_V1} with {KEY_VARIABLE} instead")
    actions = decide_client_actions(
        sites,
        plan,
        links,
        key=lambda b: (b.file, b.node.lineno, b.node.col_offset),
        # Only a module-level client used by ported calls in its own file can be split safely.
        splittable=lambda b, ported: b.top_level and b.kind == "module"
        and all(s.file == b.file and s.receiver_kind == "module_global" for s in ported),
    )

    edits: dict[str, list[Edit]] = defaultdict(list)
    helpers: dict[str, set[str]] = defaultdict(set)
    replaced: dict[str, list[tuple[int, int]]] = defaultdict(list)
    split_names: dict[str, str] = {}  # site id -> new client variable

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
        else:  # split: a second client next to the original, for the ported calls only
            new_var = f"{binding.var}_nemotron"
            statement_end = line_end(f.source, f.span(binding.statement)[1])
            indent = indentation(f.source, f.span(binding.statement)[0])
            line = indent + f"{new_var} = ".encode() + text + newline_style(f.source)
            edits[f.path].append(Edit(statement_end, statement_end, line))
            for site in action.ported:
                split_names[site.id] = new_var

    # Models, tuned settings and receivers at each ported call site.
    const_uses: dict[tuple[str, str], list[tuple[str | None, ast.Name]]] = defaultdict(list)
    text_edits: dict[str, tuple[bytes, list[Edit]]] = {}  # prompt files: path -> (original bytes, edits)
    applied: set = set()  # prompt insertions already made, so a shared prompt gets the addendum once
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

        if decision.extra_body:
            inserted = [] if any(k.arg == "extra_body" for k in call.keywords) else _insert_kwarg(f, call, f"extra_body={decision.extra_body!r}")
            if inserted:
                edits[f.path] += inserted
            else:
                decision.notes.append(f"Pass extra_body={decision.extra_body!r} here by hand")
        if decision.max_tokens:
            changed = _set_number_kwarg(f, call, ("max_tokens", "max_completion_tokens"), decision.max_tokens, floor=True)
            if changed is None:
                decision.notes.append(f"Tuned: allow at least {decision.max_tokens} output tokens (set by hand)")
            else:
                edits[f.path] += changed
        if decision.temperature is not None:
            changed = _set_number_kwarg(f, call, ("temperature",), decision.temperature, floor=False)
            if changed is None:
                decision.notes.append(f"Tuned: temperature {decision.temperature} (set by hand)")
            else:
                edits[f.path] += changed
        if decision.system_addendum and not _apply_addendum(repo, f, call, site, decision, edits, text_edits, applied):
            decision.notes.append(f"Tuned: append to the system prompt by hand: \"{decision.system_addendum}\"")

        if site.id in split_names:
            base = call.func
            while isinstance(base, ast.Attribute):
                base = base.value
            edits[f.path].append(Edit(*f.span(base), split_names[site.id].encode()))
    _port_http(repo, http_sites, plan, edits, const_uses, text_edits, applied)

    # Shared model constants: edit the constant once if every call using it moves to the same model,
    # otherwise write the model at each ported call and leave the constant for the calls that stay.
    for (path, name), uses in const_uses.items():
        f = repo.file(path)
        targets = {model for model, _ in uses}
        constant = _module_constants(f)[name]
        if len(targets) == 1 and None not in targets:
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
    vendor_dir = _vendor_dir(repo, set(helpers))
    for path, used_helpers in helpers.items():
        f = repo.file(path)
        edits[path].append(_insert_import(f, _import_line(path, vendor_dir, used_helpers)))
        edits[path] += _cleanup_sdk_imports(f, replaced[path])
    for site in sites:
        f = repo.file(site.file)
        if f and site.sdk == "anthropic" and not plan[site.id].keep:
            errors = _anthropic_error_names(f)
            if errors:
                plan[site.id].notes.append(
                    f"{site.file} handles {', '.join(errors)}: ported calls raise openai errors instead"
                )

    changed_files |= set(edits)
    # Calls sharing a string each edit it, and settings added after the same last argument each add its comma: keep one.
    output = {path: apply_edits(repo.file(path).source, list(dict.fromkeys(edits[path]))) for path in changed_files if edits[path]}
    output |= {path: apply_edits(raw, file_edits) for path, (raw, file_edits) in text_edits.items()}
    if helpers:
        output[f"{vendor_dir}/{RUNTIME_MODULE}.py".lstrip("/")] = runtime_source
    return output
