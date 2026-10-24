"""Rewrite Python code onto the OpenPort runtime: swap SDK clients, and point each ported call at its Nemotron model."""

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..runtime.openport_llm import _PROVIDER_ONLY_OPTIONS
from ..scan import _source_files
from ..scan.findings import CallSiteFinding
from ..scan.python_scanner import _constructor_sdk, _match_suffix, _module_name
from .clients import ClientAction, decide_client_actions
from .edits import Edit, apply_edits, indentation, line_end, line_starts, newline_style, quoted_like, was_comment
from .plan import SiteDecision

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
                return (self.modules[module], original) if module in self.modules else None
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
        return (self.modules[module], function) if module in self.modules else None

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
            target = self.file(self.modules.get(module))
            return [b for b in self.bindings(target) if b.kind == "module" and b.var == original] if target else []
        if kind == "factory":
            match = re.fullmatch(r"([\w.]+)\(\)", receiver)
            resolved = self.resolve_callable(f, match[1], site.function) if match else None
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
        return []


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


def port_python(
    root: Path, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], runtime_source: bytes
) -> dict[str, bytes]:
    """Rewrite the Python files under `root` for the ported sites in `plan`. Returns changed and added files.

    Updates `plan` in place: sites that can't be ported mechanically are kept, with the reason.
    """
    repo = _Repo(root)
    links = {site.id: repo.link(site) for site in sites}
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

    # Models, extra_body and receivers at each ported call site.
    const_uses: dict[tuple[str, str], list[tuple[str | None, ast.Name]]] = defaultdict(list)
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
            if any(k.arg == "extra_body" for k in call.keywords) or not (call.args or call.keywords):
                decision.notes.append(f"Pass extra_body={decision.extra_body!r} here by hand")
            else:
                last = max(f.span(n)[1] for n in [*call.args, *call.keywords])
                edits[f.path].append(Edit(last, last, f", extra_body={decision.extra_body!r}".encode()))

        if site.id in split_names:
            base = call.func
            while isinstance(base, ast.Attribute):
                base = base.value
            edits[f.path].append(Edit(*f.span(base), split_names[site.id].encode()))

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

    output = {path: apply_edits(repo.file(path).source, edits[path]) for path in changed_files if edits[path]}
    if helpers:
        output[f"{vendor_dir}/{RUNTIME_MODULE}.py".lstrip("/")] = runtime_source
    return output
