"""Rewrite TypeScript/JavaScript OpenAI and Anthropic SDK code onto Token Factory.

The OpenAI Node SDK talks to Token Factory directly: the client gets Token Factory's base URL and key,
and each ported call gets its Nemotron model. Token Factory has no Anthropic-compatible API, so an
Anthropic client becomes an `AnthropicOnNemotron` from OpenPort's runtime (openport_llm.mjs, copied into
the app), whose `messages.create` translates to chat completions and back. Plain HTTP calls (fetch, axios,
ky, got) to OpenAI-style chat endpoints move to Token Factory's URL and key (see http.py).
"""

import json
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tree_sitter import Node

from ..runtime.openport_llm import TOKEN_FACTORY_BASE_URL
from ..scan import _source_files
from ..scan.findings import CALL_SUFFIXES, CallSiteFinding, is_test_path
from ..scan.ts_scanner import FETCHES, _parser, _text, _unquote, _walk
from .clients import decide_client_actions
from .edits import Edit, apply_edits, before_newline, indentation, line_end, newline_style, quoted_like, was_comment
from .http import BASE_URL_VARIABLE, BASE_URL_VARIABLES, CHAT_URL, KEY_VARIABLE, TOKEN_FACTORY_V1, url_edits, why_kept
from .plan import SiteDecision
from .prompts import find_prompt_file, prompt_file_insertion, prompt_files_of, ts_literal_insertion

PROVIDER_ONLY = {"apiKey", "baseURL", "organization", "project", "authToken"}
EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
CLIENTS = {"OpenAI", "AzureOpenAI", "Anthropic", "AnthropicBedrock", "AnthropicVertex"}
BY_HAND = {"AzureOpenAI", "AnthropicBedrock", "AnthropicVertex"}  # their own auth: a person ports these
RUNTIME = Path(__file__).parents[1] / "runtime" / "openport_llm.mjs"
ANTHROPIC_STANDIN = "AnthropicOnNemotron"
# `?? ""` matters: with apiKey undefined, the SDK would fall back to OPENAI_API_KEY and send it to Nebius.
# NEBIUS_BASE_URL overrides the URL, as in the Python runtime; OpenPort's own evals of the ported code use it.
CONNECTION = [f'baseURL: process.env.NEBIUS_BASE_URL ?? "{TOKEN_FACTORY_BASE_URL}"', 'apiKey: process.env.NEBIUS_API_KEY ?? ""']


@dataclass
class _TsFile:
    path: str
    source: bytes
    root: Node


@dataclass
class _TsBinding:
    file: str
    var: str
    ctor: str
    new_expr: Node
    statement: Node
    top_level: bool


class _TsRepo:
    def __init__(self, root: Path):
        self.root = root
        self._files: dict[str, _TsFile | None] = {}

    def file(self, path: str | None) -> _TsFile | None:
        if not path:
            return None
        if path not in self._files:
            try:
                source = (self.root / path).read_bytes()
                tree = _parser(path.endswith((".tsx", ".jsx"))).parse(source)
                self._files[path] = _TsFile(path, source, tree.root_node)
            except OSError:
                self._files[path] = None
        return self._files[path]

    def bindings(self, f: _TsFile) -> list[_TsBinding]:
        """Where clients are built: `const client = new OpenAI()`, `this.client = new OpenAI()` in a constructor, and
        class fields (`client = new OpenAI()`), the last two named `this.client`."""
        found = []
        for node in _walk(f.root):
            if node.type == "variable_declarator":
                name, value, statement = _text(node.child_by_field_name("name")), node.child_by_field_name("value"), node.parent
            elif node.type == "assignment_expression" and _text(node.child_by_field_name("left")).startswith("this."):
                name, value, statement = _text(node.child_by_field_name("left")), node.child_by_field_name("right"), node.parent
            elif node.type == "public_field_definition":
                name, value, statement = f"this.{_text(node.child_by_field_name('name'))}", node.child_by_field_name("value"), node
            else:
                continue
            if value is not None and value.type == "call_expression":  # wrapped on the way: withTracing(new OpenAI())
                args = value.child_by_field_name("arguments")
                value = next((a for a in (args.named_children if args is not None else []) if a.type == "new_expression"), None)
            if value is None or value.type != "new_expression":
                continue
            ctor = _text(value.child_by_field_name("constructor")).split(".")[-1]
            if ctor not in CLIENTS:
                continue
            top = statement.parent.type == "program" or (
                statement.parent.type == "export_statement" and statement.parent.parent.type == "program"
            )
            found.append(_TsBinding(f.path, name, ctor, value, statement, top))
        return found

    def resolve(self, from_path: str, source: str) -> str | None:
        if source.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), source))
        elif source.startswith("@/"):
            base = "src/" + source[2:] if (self.root / "src").is_dir() else source[2:]
        else:
            return None
        candidates = [base] + [base + ext for ext in EXTENSIONS] + [f"{base}/index{ext}" for ext in EXTENSIONS]
        return next((c for c in candidates if (self.root / c).is_file()), None)

    def imports_from(self, path: str, name: str) -> bool:
        """Whether another module of the app (not its tests) imports `name` from the module at `path`, or all of it
        (`import * as config`)."""
        for found in _source_files(self.root):
            other = found.relative_to(self.root).as_posix()
            if other == path or not other.endswith(EXTENSIONS) or is_test_path(other):
                continue
            try:
                if name.encode() not in found.read_bytes():
                    continue
            except OSError:
                continue
            parsed = self.file(other)
            if parsed is None:  # it names it, and can't be read: it may import it
                return True
            for node in _walk(parsed.root):
                source = node.child_by_field_name("source") if node.type in ("import_statement", "export_statement") else None
                if source is None or self.resolve(other, _unquote(_text(source))) != path:
                    continue
                if any(n.type == "namespace_import" or n.type in ("import_specifier", "export_specifier")
                       and _text(n.child_by_field_name("name")) == name for n in _walk(node)):
                    return True
        return False

    def imported(self, f: _TsFile, name: str) -> tuple[_TsFile, str] | None:
        """Where an imported name comes from, when it's another module of the app: (file, exported name)."""
        for node in _walk(f.root):
            if node.type != "import_statement":
                continue
            source = _unquote(_text(node.child_by_field_name("source")))
            for spec in (n for n in _walk(node) if n.type == "import_specifier"):
                original, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                if _text(alias if alias is not None else original) == name:
                    target = self.file(self.resolve(f.path, source))
                    return (target, _text(original)) if target else None
        return None

    def link(self, site: CallSiteFinding) -> list[_TsBinding]:
        f = self.file(site.file)
        if f is None:
            return []
        receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
        call = _call_node(f, site)

        def serves(binding: _TsBinding) -> bool:  # a function's own client serves its calls only; `this.client`, its class's
            if binding.ctor.startswith("Anthropic") != (site.sdk == "anthropic"):
                return False
            scope = binding.new_expr.parent
            kinds = ("class_declaration", "class") if binding.var.startswith("this.") else _FUNCTION_NODES
            while scope is not None and scope.type not in kinds:
                scope = scope.parent
            return scope is None or call is not None and scope.start_byte <= call.start_byte and call.end_byte <= scope.end_byte

        local = [b for b in self.bindings(f) if b.var == receiver and serves(b)]
        if local:
            return local
        for node in _walk(f.root):  # a client imported from another module of the app
            if node.type != "import_statement":
                continue
            source = _unquote(_text(node.child_by_field_name("source")))
            for spec in (n for n in _walk(node) if n.type == "import_specifier"):
                name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                if _text(alias if alias is not None else name) == receiver:
                    target = self.file(self.resolve(f.path, source))
                    return [b for b in self.bindings(target) if b.var == _text(name) and b.top_level] if target else []
        return []


def _base_url(binding: _TsBinding) -> str:
    """The base URL a client is built with, as written: `baseURL: ...`, `{ baseURL }`, or none (the SDK's default)."""
    args = binding.new_expr.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    for child in first.named_children if first is not None and first.type == "object" else []:
        if child.type == "pair" and _unquote(_text(child.child_by_field_name("key"))) == "baseURL":
            return _text(child.child_by_field_name("value"))
        if child.type == "shorthand_property_identifier" and _text(child) == "baseURL":
            return "baseURL"
    return "the SDK's default"


_FUNCTION_NODES = {"function_declaration", "function_expression", "arrow_function", "method_definition", "generator_function_declaration"}
_NAMES = ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")


def _ts_chosen_by_caller(binding: _TsBinding) -> str | None:
    """How a client lets whoever builds it pick the provider: its options, base URL or spread-in options come from
    the function's parameters (or `this`), directly or through its locals. None when the code fixes it."""
    function = binding.new_expr.parent
    while function is not None and function.type not in _FUNCTION_NODES:
        function = function.parent
    if function is None:
        return None
    declared = function.child_by_field_name("parameters") or function.child_by_field_name("parameter")
    params = {_text(n) for n in _walk(declared) if n.type in _NAMES} if declared is not None else set()
    locals_: dict[str, Node] = {}
    for node in _walk(function):
        if node.type == "variable_declarator" and node.start_byte < binding.new_expr.start_byte and node.child_by_field_name("value"):
            for name in _walk(node.child_by_field_name("name")):  # `const x = ...` and `const { a, b } = options`
                if name.type in _NAMES:
                    locals_[_text(name)] = node.child_by_field_name("value")

    def from_caller(node: Node, depth: int = 2) -> bool:
        for n in _walk(node):
            if n.type == "this" or n.type in _NAMES and (_text(n) in params or depth and _text(n) in locals_
                                                          and from_caller(locals_[_text(n)], depth - 1)):
                return True
        return False

    args = binding.new_expr.child_by_field_name("arguments")
    first = _unwrap(args.named_children[0]) if args is not None and args.named_children else None
    if first is None:
        return None
    if first.type != "object":
        return f"`{_text(first)}`" if from_caller(first) else None
    for child in first.named_children:
        key = _unquote(_text(child.child_by_field_name("key"))) if child.type == "pair" else _text(child)
        if child.type in ("pair", "shorthand_property_identifier") and key == "baseURL" or child.type == "spread_element":
            if from_caller(child.child_by_field_name("value") if child.type == "pair" else child):
                return f"`{_text(child)}`"
    return None


def _ts_foreign_client(f: _TsFile | None, site: CallSiteFinding) -> str | None:
    """Why a client the port didn't find stays: it's another class (`this.client = new GroqClient()`), or the object
    was handed it when built (`this.client = client` in its constructor). None when neither."""
    call = _call_node(f, site) if f else None
    receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
    scope = call
    kinds = ("class_declaration", "class") if receiver.startswith("this.") else _FUNCTION_NODES
    while scope is not None and scope.type not in kinds:
        scope = scope.parent
    for node in _walk(scope) if scope is not None else []:
        if node.type == "assignment_expression" and _text(node.child_by_field_name("left")) == receiver:
            value = node.child_by_field_name("right")
        elif node.type == "variable_declarator" and _text(node.child_by_field_name("name")) == receiver:
            value = node.child_by_field_name("value")
        elif node.type == "public_field_definition" and f"this.{_text(node.child_by_field_name('name'))}" == receiver:
            value = node.child_by_field_name("value")  # a class field: `client = new GroqClient()`
        else:
            continue
        value = _unwrap(value)
        if value is not None and value.type == "new_expression":
            ctor = _text(value.child_by_field_name("constructor")).split(".")[-1]
            if ctor not in CLIENTS:
                return f"its client is a `{ctor}`, not one of OpenAI's or Anthropic's SDK clients: port it by hand"
        method = node.parent
        while method is not None and method.type not in _FUNCTION_NODES:
            method = method.parent
        declared = method.child_by_field_name("parameters") if method is not None else None
        if value is not None and value.type == "identifier" and declared is not None and _text(value) in {
                _text(n) for n in _walk(declared) if n.type in _NAMES}:
            named = _text(method.child_by_field_name("name"))
            where = f"`{named}`" if named else "the function that sets it"
            return (f"its client is whatever builds the object passes (the `{_text(value)}` parameter of {where}): "
                    "port the code that builds that client instead")
    return None


def _ts_parameter_client(f: _TsFile | None, site: CallSiteFinding) -> str | None:
    """The parameter a call's client arrives through, when it's a parameter of the function the call is in."""
    receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
    function = _call_node(f, site) if f else None
    while function is not None and function.type not in _FUNCTION_NODES:
        function = function.parent
    declared = (function.child_by_field_name("parameters") or function.child_by_field_name("parameter")) if function else None
    names = {_text(n) for n in _walk(declared) if n.type in _NAMES} if declared is not None else set()
    return receiver if receiver in names else None


def _same(a: Node | None, b: Node | None) -> bool:
    return a is not None and b is not None and (a.start_byte, a.end_byte, a.type) == (b.start_byte, b.end_byte, b.type)


def _shadowed(node: Node, name: str, scope: Node, own: Node) -> bool:
    """Whether `name` at `node` is a function's own parameter or variable, between it and `scope` (where the client
    built by `own` is declared), not that client."""
    function = node.parent
    while function is not None and not _same(function, scope):
        if function.type in _FUNCTION_NODES:
            declared = function.child_by_field_name("parameters") or function.child_by_field_name("parameter")
            if declared is not None and name in {_text(n) for n in _walk(declared) if n.type in _NAMES}:
                return True
            if any(n.type == "variable_declarator" and _text(n.child_by_field_name("name")) == name
                   and not _same(n.child_by_field_name("value"), own) for n in _walk(function)):
                return True
        function = function.parent
    return False


def _ts_other_uses(f: _TsFile, binding: _TsBinding, linked: Callable[[int], bool]) -> list[str]:
    """What else the file that builds a client does with it besides the calls the port linked to it:
    `openai.models.list()`, `client.beta.files...`, a call the port couldn't trace to it. As in the Python port, each
    counts as a call that stays. (An exported client's importers aren't followed.) `linked(line)` says whether the
    call on that line is one of the client's own."""
    if binding.var.startswith("this."):
        scope = binding.new_expr.parent
        while scope is not None and scope.type not in ("class_declaration", "class"):
            scope = scope.parent
        refs = [n for n in _walk(scope) if n.type == "member_expression" and re.sub(r"\s", "", _text(n)) == binding.var] if scope else []
    else:
        scope = None if binding.top_level else binding.new_expr.parent
        while scope is not None and scope.type not in _FUNCTION_NODES:
            scope = scope.parent
        refs = [n for n in _walk(scope or f.root) if n.type == "identifier" and _text(n) == binding.var
                and not _shadowed(n, binding.var, scope or f.root, binding.new_expr)]
    suffixes = {suffix for suffix, _, _ in CALL_SUFFIXES}
    found: list[str] = []
    for ref in refs:
        names, outer = [], ref
        while (parent := outer.parent) is not None:
            if parent.type == "member_expression" and _same(parent.child_by_field_name("object"), outer):
                names.append(_text(parent.child_by_field_name("property")))
            elif parent.type == "call_expression" and _same(parent.child_by_field_name("function"), outer) and names[-1:] == ["withOptions"]:
                names.pop()  # the SDK's copy of the same client
            elif parent.type != "non_null_expression":
                break
            outer = parent
        while names[:1] == ["beta"]:
            names.pop(0)
        if not names:
            continue
        parent = outer.parent
        called = parent is not None and parent.type == "call_expression" and _same(parent.child_by_field_name("function"), outer)
        if called and tuple(names) in suffixes and linked(parent.start_point[0] + 1):
            continue
        written = re.sub(r"\s+", "", _text(outer))
        use = f"`{written}` ({f.path}:{outer.start_point[0] + 1})"
        if use not in found:
            found.append(use)
    return found


def _ts_read_only_at(repo: "_TsRepo", f: _TsFile, name: str, uses: list[Node]) -> bool:
    """Whether a top-level constant is read nowhere but at `uses`: not elsewhere in its file (an `export { MODEL }`
    counts), and imported by no other module of the app."""
    used = {(n.start_byte, n.end_byte) for n in uses}
    for node in _walk(f.root):
        if node.type in ("identifier", "shorthand_property_identifier") and _text(node) == name and (node.start_byte, node.end_byte) not in used:
            if not (node.parent is not None and node.parent.type == "variable_declarator" and _same(node.parent.child_by_field_name("name"), node)):
                return False
    return not repo.imports_from(f.path, name)


def _call_node(f: _TsFile, site: CallSiteFinding) -> Node | None:
    for node in _walk(f.root):
        if node.type == "call_expression" and node.start_point[0] + 1 == site.line:
            parts = [p for p in re.sub(r"\s+|\?|!", "", _text(node.child_by_field_name("function"))).split(".") if p]
            for suffix, _, api in CALL_SUFFIXES:
                if api == site.api and tuple(parts[-len(suffix):]) == suffix:
                    return node
    return None


def _properties(call: Node) -> dict[str, Node]:
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    if first is None or first.type != "object":
        return {}
    return {
        _unquote(_text(child.child_by_field_name("key"))): child.child_by_field_name("value")
        for child in first.named_children
        if child.type == "pair"
    }


def _top_level_strings(f: _TsFile) -> dict[str, Node]:
    constants = {}
    for statement in f.root.named_children:
        declaration = statement.named_children[0] if statement.type == "export_statement" and statement.named_children else statement
        if declaration.type in ("lexical_declaration", "variable_declaration"):
            for declarator in declaration.named_children:
                value = declarator.child_by_field_name("value") if declarator.type == "variable_declarator" else None
                if value is not None and value.type == "string":
                    constants[_text(declarator.child_by_field_name("name"))] = value
    return constants


_WRAPPERS = ("as_expression", "satisfies_expression", "parenthesized_expression", "non_null_expression")


def _unwrap(node: Node | None) -> Node | None:
    """`messages as any`, `(x)`, `x!` -> the expression inside."""
    while node is not None and node.type in _WRAPPERS and node.named_children:
        node = node.named_children[0]
    return node


def _declared(f: _TsFile, name: str, before: int | None = None) -> Node | None:
    """The value of the declaration of `name` (the last one before `before`, if given)."""
    best = None
    for node in _walk(f.root):
        if node.type == "variable_declarator" and _text(node.child_by_field_name("name")) == name:
            if before is None or node.start_byte < before:
                best = node
    return best.child_by_field_name("value") if best is not None else None


def _object_value(obj: Node, key: str) -> Node | None:
    for child in obj.named_children:
        if child.type == "pair" and _unquote(_text(child.child_by_field_name("key"))) == key:
            return child.child_by_field_name("value")
    return None


def _system_literal(repo: _TsRepo, f: _TsFile, call: Node, sdk: str = "openai",
                    properties: dict[str, Node] | None = None) -> tuple[_TsFile, Node] | None:
    """Follow a call's system prompt back to the string or template literal it's written in: the first message
    on OpenAI's SDK (or in a plain HTTP call's request body, given as `properties`), the `system` property on
    Anthropic's."""
    properties = properties if properties is not None else _properties(call)
    if sdk == "anthropic":
        content = _unwrap(properties.get("system"))
    else:
        messages = _unwrap(properties.get("messages"))
        if messages is not None and messages.type in ("identifier", "shorthand_property_identifier"):
            messages = _unwrap(_declared(f, _text(messages), call.start_byte))
        if messages is None or messages.type != "array" or not messages.named_children:
            return None
        first = messages.named_children[0]
        role = _object_value(first, "role") if first.type == "object" else None
        if role is None or _unquote(_text(role)) not in ("system", "developer"):
            return None
        content = _unwrap(_object_value(first, "content"))
    for _ in range(4):
        if content is None:
            return None
        if content.type in ("string", "template_string"):
            return f, content
        if content.type != "identifier":
            return None
        name = _text(content)
        value = _declared(f, name)
        if value is None:
            origin = repo.imported(f, name)
            if origin is None:
                return None
            f, name = origin
            value = _declared(f, name)
        content = _unwrap(value)
    return None


def _set_number_property(f: _TsFile, call: Node, names: tuple[str, ...], value: float, floor: bool) -> list[Edit] | None:
    """Set a numeric request property (or raise it to `value`, if `floor`). None when it can't be done safely."""
    args = call.child_by_field_name("arguments")
    obj = args.named_children[0] if args is not None and args.named_children else None
    return _set_number_in(f, obj, names, value, floor)


def _set_number_in(f: _TsFile, obj: Node | None, names: tuple[str, ...], value: float, floor: bool) -> list[Edit] | None:
    """Set a numeric property of an object literal (or raise it to `value`, if `floor`)."""
    if obj is None or obj.type != "object":
        return None
    for name in names:
        current = _object_value(obj, name)
        if current is not None:
            if current.type != "number":
                return None
            number = float(_text(current))
            new = max(number, value) if floor else value
            return [] if new == number else [Edit(current.start_byte, current.end_byte, f"{new:g}".encode())]
    return _add_property(f, obj, f"{names[0]}: {value:g}")


def _add_property(f: _TsFile, obj: Node, text: str, comment: str | None = None) -> list[Edit] | None:
    """Add `text` as the object's last property, on its own line when the object spans lines. A `comment`
    line goes above it, so it needs that layout: None when it can't be placed."""
    entries = [c for c in obj.named_children if c.type != "comment"]
    if not entries:
        return None if comment else [Edit(obj.start_byte + 1, obj.start_byte + 1, f" {text} ".encode())]
    last = entries[-1]
    line_start = f.source.rfind(b"\n", 0, last.start_byte) + 1
    if f.source[line_start:last.start_byte].strip():
        return None if comment else [Edit(last.end_byte, last.end_byte, f", {text}".encode())]
    nl, indent = newline_style(f.source), f.source[line_start:last.start_byte]
    end = before_newline(f.source, last.end_byte)
    comma = [] if f.source[last.end_byte:end].lstrip().startswith(b",") else [Edit(last.end_byte, last.end_byte, b",")]
    lead = nl + indent + f"// {comment}".encode() if comment else b""
    return comma + [Edit(end, end, lead + nl + indent + text.encode() + b",")]


def _ts_value(value) -> str:
    """A JSON value written as a TypeScript literal: `{ enable_thinking: false }`."""
    if isinstance(value, dict):
        items = ", ".join(f"{k if re.fullmatch(r'[A-Za-z_$][\w$]*', k) else json.dumps(k)}: {_ts_value(v)}" for k, v in value.items())
        return f"{{ {items} }}"
    return json.dumps(value)


def _set_extra_body(f: _TsFile, call: Node, extra_body: dict, sdk: str = "openai") -> list[Edit] | None:
    """Write Token Factory-only request options (Nemotron's reasoning switch) into the call. The OpenAI Node SDK
    sends unknown properties as they are; its types don't know them, hence the @ts-expect-error. OpenPort's stand-in
    for Anthropic's SDK takes them in `extra_body`, as the Python one does, and its types aren't checked: there, an
    @ts-expect-error would have no error to expect, which is an error itself."""
    args = call.child_by_field_name("arguments")
    obj = args.named_children[0] if args is not None and args.named_children else None
    if obj is None or obj.type != "object" or any(_object_value(obj, key) is not None for key in [*extra_body, "extra_body"]):
        return None
    if sdk == "anthropic":
        return _add_property(f, obj, f"extra_body: {_ts_value(extra_body)}")
    edits: list[Edit] = []
    for key, value in extra_body.items():
        added = _add_property(f, obj, f"{key}: {_ts_value(value)}", "@ts-expect-error Token Factory option (Nemotron's reasoning switch), not in the SDK's types")
        if added is None:
            return None
        edits += added
    return edits


def _arguments_text(f: _TsFile, binding: _TsBinding) -> bytes:
    """New constructor arguments: Token Factory's URL and key, plus every other option the original passed. Spread
    options go first, so they can't override the URL or key."""
    args = binding.new_expr.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    spread: list[str] = []
    kept: list[str] = []
    if first is not None and first.type == "object":
        for child in first.named_children:
            name = _unquote(_text(child.child_by_field_name("key"))) if child.type == "pair" else _text(child)
            if child.type in ("pair", "shorthand_property_identifier") and name in PROVIDER_ONLY:
                continue  # `baseURL` and `{ apiKey }` alike
            (spread if child.type == "spread_element" else kept).append(_text(child) if child.type == "comment" else _text(child) + ",")
    elif first is not None:  # options passed in as a variable
        spread.append(f"...{_text(first)},")

    entries = spread + [c + "," for c in CONNECTION] + kept
    if first is not None and "\n" in _text(first):
        base = indentation(f.source, binding.statement.start_byte).decode()
        own = next((c for c in first.named_children if f.source.rfind(b"\n", 0, c.start_byte) > first.start_byte), None)
        inner = indentation(f.source, own.start_byte).decode() if own is not None else base + "  "  # the file's own indent
        nl = newline_style(f.source).decode()
        return ("({" + nl + "".join(f"{inner}{e}{nl}" for e in entries) + base + "})").encode()
    return ("({ " + " ".join(entries).rstrip(",") + " })").encode()


def _imports(f: _TsFile) -> list[Node]:
    return [node for node in f.root.named_children if node.type == "import_statement"]


def _runtime(paths: list[str]) -> tuple[str, bytes]:
    """Where the runtime goes, and its text: next to the first file that needs it, as TypeScript (left unchecked)
    in a TypeScript file's folder, else as an ES module."""
    folder = posixpath.dirname(paths[0])
    source = RUNTIME.read_bytes()
    if paths[0].endswith(TS_EXTENSIONS):
        # Typed loosely: it stands where the app's code expects the Anthropic SDK's client (a field declared
        # `client!: Anthropic`, a parameter), and the SDK's client has far more members than the calls it runs.
        declaration = b"export class AnthropicOnNemotron {"
        assert source.count(declaration) == 1, "the runtime's stand-in moved"
        nl = newline_style(source)
        source = (source.replace(declaration, b"class AnthropicOnNemotronClient {")
                  + nl + b"export const AnthropicOnNemotron: any = AnthropicOnNemotronClient;" + nl)
        return posixpath.join(folder, "openport_llm.ts"), b"// @ts-nocheck: OpenPort's runtime, written as plain JavaScript" + nl + source
    return posixpath.join(folder, "openport_llm.mjs"), source


def _node_resolution(repo: _TsRepo, path: str) -> str | None:
    """For a TypeScript file whose project resolves imports as Node does (`"module"` or `"moduleResolution"` node16
    or nodenext, in the nearest tsconfig.json or one it extends), the extension a relative import needs: `.ts` with
    allowImportingTsExtensions, else `.js`. None for a bundler's resolution, where none is needed."""
    folder = posixpath.dirname(path)
    while True:
        config = repo.root / folder / "tsconfig.json"
        for _ in range(5):  # a few `extends` deep
            try:
                text = config.read_text(encoding="utf-8", errors="replace")
            except OSError:
                break
            if re.search(r'"module(?:Resolution)?"\s*:\s*"node(?:16|next)"', text, re.IGNORECASE):
                return ".ts" if re.search(r'"allowImportingTsExtensions"\s*:\s*true', text) else ".js"
            base = re.search(r'"extends"\s*:\s*"(\.{1,2}/[^"]+)"', text)
            if base is None:
                break
            config = config.parent / (base[1] if base[1].endswith(".json") else base[1] + ".json")
        if (repo.root / folder / "tsconfig.json").is_file() or not folder:
            return None
        folder = posixpath.dirname(folder)


def _import_edit(repo: _TsRepo, f: _TsFile, runtime_path: str) -> Edit:
    """`import { AnthropicOnNemotron } from "./openport_llm";` after the file's last import. The path has an
    extension where ES modules need one: the one the file's own relative imports use (`.ts`, `.js` or none), or, when
    it has none, the one its project's module resolution needs."""
    relative = posixpath.relpath(runtime_path, posixpath.dirname(f.path) or ".")
    relative = relative if relative.startswith(".") else "./" + relative
    if relative.endswith(".ts"):
        own = [s for node in _imports(f) if (s := _unquote(_text(node.child_by_field_name("source")))).startswith(".")]
        extension = next((e for e in (".ts", ".js") if any(s.endswith(e) for s in own)), "")
        if not own:
            extension = _node_resolution(repo, f.path) or ""
        relative = relative[: -len(".ts")] + extension
    position = line_end(f.source, _imports(f)[-1].end_byte)
    return Edit(position, position, f'import {{ {ANTHROPIC_STANDIN} }} from "{relative}";'.encode() + newline_style(f.source))


# --- plain HTTP calls -------------------------------------------------------------------------------

PUBLIC_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "PUBLIC_")  # what bundlers need to expose a variable to the browser


def _http_call_node(f: _TsFile, site: CallSiteFinding) -> Node | None:
    for node in _walk(f.root):
        if node.type == "call_expression" and node.start_point[0] + 1 == site.line:
            name = re.sub(r"\s+|\?|!", "", _text(node.child_by_field_name("function")))
            if name.split(".")[-1] in FETCHES or name.endswith(".post"):
                return node
    return None


def _value_of(repo: _TsRepo, f: _TsFile, node: Node | None) -> tuple[_TsFile, Node | None]:
    """An identifier's declared value (in this file, or the app module it's imported from); anything else as it is."""
    node = _unwrap(node)
    if node is None or node.type not in ("identifier", "shorthand_property_identifier"):
        return f, node
    name = _text(node)
    value = _declared(f, name, node.start_byte) or _declared(f, name)
    if value is None and (origin := repo.imported(f, name)):
        f, value = origin[0], _declared(*origin)
    return f, _unwrap(value)


def _ts_reaching(repo: _TsRepo, f: _TsFile, node: Node, depth: int = 3) -> list[tuple[_TsFile, Node]]:
    """An expression and the values it's built from, each name followed to its declaration."""
    found = [(f, node)]
    names: dict[str, Node] = {}
    for n in _walk(node) if depth else []:
        if n.type in ("identifier", "shorthand_property_identifier"):
            names.setdefault(_text(n), n)
    for name in names.values():
        target, value = _value_of(repo, f, name)
        if value is not None:
            found += _ts_reaching(repo, target, value, depth - 1)
    return found


def _ts_literals(node: Node) -> list[Node]:
    """The string literals in an expression; a template string counts as one."""
    if node.type in ("string", "template_string"):
        return [node]
    return [literal for child in node.named_children for literal in _ts_literals(child)]


def _ts_env_names(node: Node) -> list[Node]:
    """Where an expression names the environment variables it reads: process.env.X, process.env["X"],
    import.meta.env.X, Deno.env.get("X")."""
    found = []
    for n in _walk(node):
        target = None
        if n.type == "member_expression" and re.sub(r"\s", "", _text(n.child_by_field_name("object"))) in ("process.env", "import.meta.env"):
            target = n.child_by_field_name("property")
        elif n.type == "subscript_expression" and re.sub(r"\s", "", _text(n.child_by_field_name("object"))) == "process.env":
            target = n.child_by_field_name("index")
        elif n.type == "call_expression" and re.sub(r"\s", "", _text(n.child_by_field_name("function"))) == "Deno.env.get":
            args = n.child_by_field_name("arguments")
            target = args.named_children[0] if args is not None and args.named_children else None
        if target is not None and target.type in ("property_identifier", "string"):
            found.append(target)
    return found


def _variable(node: Node) -> str:
    return _unquote(_text(node))


def _renamed(node: Node, new: str) -> Edit:
    """Rename the environment variable `node` names, keeping a bundler's prefix (VITE_NEBIUS_API_KEY)."""
    new = next((p for p in PUBLIC_PREFIXES if _variable(node).startswith(p)), "") + new
    return Edit(node.start_byte, node.end_byte, new.encode() if node.type == "property_identifier" else quoted_like(node.text, new))


def _object_properties(obj: Node | None) -> dict[str, Node]:
    """An object literal's properties: `key: value` pairs, and `{ headers }` shorthand (as the name)."""
    properties = {}
    for child in obj.named_children if obj is not None and obj.type == "object" else []:
        if child.type == "pair":
            properties[_unquote(_text(child.child_by_field_name("key")))] = child.child_by_field_name("value")
        elif child.type == "shorthand_property_identifier":
            properties[_text(child)] = child
    return properties


def _body(repo: _TsRepo, f: _TsFile, node: Node | None) -> tuple[_TsFile, Node] | None:
    """A request body as the object literal it's written as: through its variable, and JSON.stringify(...)."""
    f, node = _value_of(repo, f, node)
    if node is not None and node.type == "call_expression" and re.sub(r"\s", "", _text(node.child_by_field_name("function"))) == "JSON.stringify":
        inner = node.child_by_field_name("arguments")
        f, node = _value_of(repo, f, inner.named_children[0] if inner is not None and inner.named_children else None)
    return (f, node) if node is not None and node.type == "object" else None


@dataclass
class _TsHttpCall:
    """A plain HTTP call, and what it's built from."""

    call: Node
    url: Node
    body: tuple[_TsFile, Node] | None  # the request body, when it's an object literal
    url_literals: list[tuple[_TsFile, Node]]  # the strings its URL is made of
    url_variables: list[tuple[_TsFile, Node]]  # the OpenAI SDK's base URL variables it reads
    authorization: Node | None  # its Authorization header's value, when OpenPort can see it
    key_variables: list[tuple[_TsFile, Node]]  # the environment variables that header reads


def _ts_unique(pairs: list[tuple[_TsFile, Node]]) -> list[tuple[_TsFile, Node]]:
    return list({(f.path, n.start_byte, n.end_byte): (f, n) for f, n in pairs}.values())


def _ts_http_call(repo: _TsRepo, f: _TsFile, site: CallSiteFinding) -> _TsHttpCall | None:
    call = _http_call_node(f, site)
    args = call.child_by_field_name("arguments") if call is not None else None
    if args is None or not args.named_children:
        return None
    url, rest = args.named_children[0], [_value_of(repo, f, arg) for arg in args.named_children[1:]]
    options = [(t, o) for t, o in rest if o is not None and o.type == "object"]

    # The body: fetch's `body: JSON.stringify(payload)`, axios' payload, or ky's and got's `json: payload`.
    body = None
    if re.sub(r"\s+|\?|!", "", _text(call.child_by_field_name("function"))).split(".")[-1] in FETCHES:
        if rest and rest[0][1] is not None and rest[0][1].type == "object":
            properties = _object_properties(rest[0][1])
            body = _body(repo, rest[0][0], properties.get("body") or properties.get("json"))
    elif rest:
        properties = _object_properties(rest[0][1])
        if "json" in properties and "model" not in properties:
            body = _body(repo, rest[0][0], properties["json"])
        elif rest[0][1] is not None:
            body = _body(repo, *rest[0])

    reached = _ts_reaching(repo, f, url)
    literals = _ts_unique([(t, lit) for t, node in reached for lit in _ts_literals(node)])
    variables = _ts_unique([(t, n) for t, node in reached for n in _ts_env_names(node) if _variable(n) in BASE_URL_VARIABLES])
    authorization, keys = None, []
    for target, obj in options:
        headers_file, headers = _value_of(repo, target, _object_properties(obj).get("headers"))
        if headers is not None and headers.type == "new_expression":  # new Headers({...})
            inner = headers.child_by_field_name("arguments")
            headers = _unwrap(inner.named_children[0]) if inner is not None and inner.named_children else None
        authorization = next((value for name, value in _object_properties(headers).items() if name.lower() == "authorization"), None)
        if authorization is not None:
            keys = _ts_unique([(t, n) for t, node in _ts_reaching(repo, headers_file, authorization) for n in _ts_env_names(node)])
            break
    return _TsHttpCall(call, url, body, literals, variables, authorization, keys)


def _split_ts_url(f: _TsFile, url: Node) -> list[Edit]:
    """Token Factory's URL for a call whose URL also serves calls that stay: a second constant beside the top-level
    constant it names (CHAT_URL_NEMOTRON), as a shared client is split, or else the URL itself at the call."""
    name = _text(url) if url.type == "identifier" else None
    statement = None
    for node in f.root.named_children if name else []:
        declaration = node.named_children[0] if node.type == "export_statement" and node.named_children else node
        if declaration.type in ("lexical_declaration", "variable_declaration") and any(
                d.type == "variable_declarator" and _text(d.child_by_field_name("name")) == name for d in declaration.named_children):
            statement = node
    if statement is None:
        return [Edit(url.start_byte, url.end_byte, f'"{CHAT_URL}"'.encode())]
    new = name + ("_NEMOTRON" if name.isupper() else "Nemotron")
    position = line_end(f.source, statement.end_byte)
    nl = newline_style(f.source)
    prefix = nl if position == len(f.source) and not f.source.endswith(b"\n") else b""
    return [Edit(position, position, prefix + f'const {new} = "{CHAT_URL}";'.encode() + nl), Edit(url.start_byte, url.end_byte, new.encode())]


def _ts_http_calls(repo: _TsRepo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision]) -> dict[str, _TsHttpCall | None]:
    """Each plain HTTP call, and which can move at all: one whose URL OpenPort can't see stays where it is."""
    calls = {site.id: _ts_http_call(repo, f, site) if (f := repo.file(site.file)) else None for site in sites}
    for site in sites:
        decision, http = plan[site.id], calls[site.id]
        if decision.keep:
            continue
        if http is None:
            decision.keep_because("couldn't locate the call in the source")
            continue
        reason = why_kept([_text(node) for _, node in http.url_literals], _text(http.url))
        if reason:
            decision.keep_because(reason)
    return calls


def _ts_kept_prompts(repo: _TsRepo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], http_calls: dict) -> set:
    """The prompt literals calls that stay use, as (file, offset): an addendum tuned for a call that moves isn't
    written into those."""
    found = set()
    for site in sites:
        f = repo.file(site.file)
        if not plan[site.id].keep:
            continue
        found |= prompt_files_of(repo.root, plan[site.id].system_prompts)  # the prompt files it was seen sending
        if f is None:
            continue
        if site.sdk == "http":
            http = http_calls.get(site.id)
            body_file, body = http.body if http and http.body else (f, None)
            resolved = _system_literal(repo, body_file, body, "openai", _object_properties(body)) if body is not None else None
        else:
            call = _call_node(f, site)
            resolved = _system_literal(repo, f, call, site.sdk) if call is not None else None
        if resolved:
            found.add((resolved[0].path, resolved[1].start_byte))
    return found


def _port_ts_http(repo: _TsRepo, sites: list[CallSiteFinding], plan: dict[str, SiteDecision], calls: dict[str, _TsHttpCall | None],
                  edits: dict, const_uses: dict, text_edits: dict, applied: set, kept_prompts: set) -> None:
    """Point each ported plain HTTP call at Token Factory: its URL, its key and its model, where the code spells
    them out (`calls` from _ts_http_calls)."""

    def key(f: _TsFile, node: Node) -> tuple[str, int, int]:
        return f.path, node.start_byte, node.end_byte

    shared = {key(f, node) for s in sites if plan[s.id].keep and calls[s.id]  # what calls that stay use too: left alone
              for f, node in [*calls[s.id].url_literals, *calls[s.id].url_variables, *calls[s.id].key_variables]}

    for site in sites:
        f, decision, http = repo.file(site.file), plan[site.id], calls[site.id]
        body_file, body = http.body if http and http.body else (f, None)
        model = _object_properties(body).get("model")
        constants = _top_level_strings(body_file) if body is not None else {}
        if model is not None and model.type == "identifier" and _text(model) in constants:
            const_uses[(body_file.path, _text(model))].append((decision.model, model))  # kept calls count too (model None)
        if decision.keep:
            continue

        # The URL: the strings it's built from, unless calls that stay use them too.
        texts = [_text(node) for _, node in http.url_literals]
        changes = url_edits(texts)
        if any(key(*http.url_literals[i]) in shared for i in changes):
            edits[f.path] += _split_ts_url(f, http.url)
        else:
            for i, text in changes.items():
                target, node = http.url_literals[i]
                edits[target.path].append(Edit(node.start_byte, node.end_byte, text.encode()))
            for target, node in http.url_variables:
                if key(target, node) not in shared and not any(
                        target is t and n.start_byte <= node.start_byte and node.end_byte <= n.end_byte
                        for i, (t, n) in enumerate(http.url_literals) if i in changes):
                    edits[target.path].append(_renamed(node, BASE_URL_VARIABLE))

        # The key, where it's read from the environment.
        for target, node in http.key_variables:
            if key(target, node) in shared:
                decision.notes.append(f"Its key ({_variable(node)}) is read for calls that stay too: send {KEY_VARIABLE} here by hand")
            elif not _variable(node).endswith(KEY_VARIABLE):
                edits[target.path].append(_renamed(node, KEY_VARIABLE))
        if http.authorization is None:
            decision.notes.append(f"Send {KEY_VARIABLE} as its bearer token: OpenPort couldn't find its Authorization header")
        elif not http.key_variables:
            written = not any(n.type in ("identifier", "member_expression", "call_expression") for n in _walk(http.authorization))
            source = "is written in the code" if written else f"comes from `{_text(http.authorization)}`"
            decision.notes.append(f"Its key {source}: send {KEY_VARIABLE} instead")

        # The model and tuned settings, in the request body.
        if body is None:
            decision.notes.append(f"OpenPort couldn't see its request body: make sure it sends model {decision.model}")
            continue
        if model is None:
            decision.notes.append(f"No `model` in its request body: make sure {decision.model} is sent")
        elif model.type == "string":
            edits[body_file.path].append(Edit(model.start_byte, model.end_byte, quoted_like(model.text, decision.model)))
            comment = was_comment(body_file.source, model.end_byte, _unquote(_text(model)), b"//")
            if comment:
                edits[body_file.path].append(comment)
        elif not (model.type == "identifier" and _text(model) in constants):
            decision.notes.append(f"Model comes from `{_text(model)}`: set it to {decision.model}")
        if decision.extra_body:  # a plain object: no SDK types to tell about Token Factory's options
            added: list[Edit] | None = None if any(k in _object_properties(body) for k in decision.extra_body) else []
            for k, v in decision.extra_body.items() if added is not None else []:
                added += _add_property(body_file, body, f"{k}: {_ts_value(v)}") or []
            if not added:
                decision.notes.append(f"Send {decision.extra_body!r} in its request body by hand")
            else:
                edits[body_file.path] += added
        for names, number, floor, label in (
            (("max_tokens", "max_completion_tokens"), decision.max_tokens, True, "allow at least {} output tokens"),
            (("temperature",), decision.temperature, False, "temperature {}"),
        ):
            if number is None or (floor and not number):
                continue
            changed = _set_number_in(body_file, body, names, number, floor)
            if changed is None:
                decision.notes.append(f"Tuned: {label.format(number)} (set by hand)")
            else:
                edits[body_file.path] += changed
        if decision.system_addendum and (note := _apply_ts_addendum(repo, body_file, body, "openai", decision, edits, text_edits, applied,
                                                                     kept_prompts, _object_properties(body))):
            decision.notes.append(note)


def port_typescript(root: Path, sites: list[CallSiteFinding], plan: dict[str, SiteDecision]) -> dict[str, bytes]:
    """Rewrite the TypeScript/JavaScript files under `root` for the ported OpenAI and Anthropic sites in `plan`."""
    repo = _TsRepo(root)
    http_sites = [site for site in sites if site.sdk == "http"]
    sites = [site for site in sites if site.sdk != "http"]
    links = {site.id: repo.link(site) for site in sites}
    for site in sites:
        by_hand = [b for b in links[site.id] if b.ctor in BY_HAND]
        if by_hand and not plan[site.id].keep:
            plan[site.id].keep_because(f"built with {by_hand[0].ctor}: port by hand")
            links[site.id] = []
        anthropic = [b for b in links[site.id] if b.ctor == "Anthropic"]
        if anthropic and not plan[site.id].keep and not _imports(repo.file(anthropic[0].file)):
            plan[site.id].keep_because("a CommonJS module, and OpenPort's runtime for Anthropic code is an ES module: port it by hand")
            links[site.id] = []
        urls = sorted({_base_url(b) for b in links[site.id]}) if len(links[site.id]) > 1 else []
        chosen = next((how for b in links[site.id] if (how := _ts_chosen_by_caller(b))), None)
        if plan[site.id].keep:  # where the code picks no single provider, as in the Python port
            continue
        if not links[site.id] and (parameter := _ts_parameter_client(repo.file(site.file), site)):
            plan[site.id].keep_because(f"its client is whatever its caller passes (the `{parameter}` parameter): port the code that "
                                       "builds that client instead")
        elif not links[site.id] and (why := _ts_foreign_client(repo.file(site.file), site)):
            plan[site.id].keep_because(why)
        if len(urls) > 1:
            plan[site.id].keep_because(f"its client is one of several, for different providers (base URLs {', '.join(urls)}): "
                                       "add Token Factory among them by hand")
        elif chosen:
            plan[site.id].keep_because(f"its client takes its provider from whoever builds it ({chosen}): point that at "
                                       f"{TOKEN_FACTORY_V1} with {KEY_VARIABLE} instead")

    def key(binding: _TsBinding) -> tuple:
        return binding.file, binding.new_expr.start_byte

    linked = {(site.file, site.line, key(b)) for site in sites for b in links[site.id]}
    other_uses: dict[tuple, list[str]] = {}

    def held(binding: _TsBinding) -> list[str]:  # what else the client does: it stays on the original provider for that
        if key(binding) not in other_uses:
            other_uses[key(binding)] = _ts_other_uses(repo.file(binding.file), binding, lambda line: (binding.file, line, key(binding)) in linked)
        return other_uses[key(binding)]

    actions = decide_client_actions(
        sites,
        plan,
        links,
        key=key,
        splittable=lambda b, ported: b.top_level and all(s.file == b.file for s in ported),
        other_uses=held,
    )

    edits: dict[str, list[Edit]] = defaultdict(list)
    split_names: dict[str, str] = {}
    standins: set[str] = set()  # files whose Anthropic client becomes OpenPort's stand-in
    for action in actions.values():
        binding: _TsBinding = action.binding
        f = repo.file(binding.file)
        arguments = _arguments_text(f, binding)
        anthropic = binding.ctor == "Anthropic"
        if anthropic:
            standins.add(f.path)
        if action.action == "replace":
            if anthropic:
                ctor = binding.new_expr.child_by_field_name("constructor")
                edits[f.path].append(Edit(ctor.start_byte, ctor.end_byte, ANTHROPIC_STANDIN.encode()))
            args = binding.new_expr.child_by_field_name("arguments")
            if args is not None:
                edits[f.path].append(Edit(args.start_byte, args.end_byte, arguments))
            else:  # `new OpenAI` with no parentheses
                end = binding.new_expr.end_byte
                edits[f.path].append(Edit(end, end, arguments))
        else:
            new_var = f"{binding.var}Nemotron"
            keyword = _text(binding.statement).split()[0]  # const / let / var
            nl = newline_style(f.source)
            indent = indentation(f.source, binding.statement.start_byte)
            # The constructor as the file names it: `oai.OpenAI` under `import * as oai`, or an alias.
            ctor = ANTHROPIC_STANDIN if anthropic else _text(binding.new_expr.child_by_field_name("constructor"))
            line = indent + f"{keyword} {new_var} = new {ctor}".encode() + arguments + b";" + nl
            position = line_end(f.source, binding.statement.end_byte)
            edits[f.path].append(Edit(position, position, line))
            for site in action.ported:
                split_names[site.id] = new_var

    const_uses: dict[tuple[str, str], list[tuple[str | None, Node]]] = defaultdict(list)
    text_edits: dict[str, tuple[bytes, list[Edit]]] = {}
    applied: set = set()
    http_calls = _ts_http_calls(repo, http_sites, plan)
    kept_prompts = _ts_kept_prompts(repo, [*sites, *http_sites], plan, http_calls)
    for site in sites:
        f = repo.file(site.file)
        decision = plan[site.id]
        call = _call_node(f, site) if f else None
        if call is None:
            if not decision.keep:
                decision.keep_because("couldn't locate the call in the source")
            continue
        value = _properties(call).get("model")
        constants = _top_level_strings(f)
        if value is not None and value.type == "identifier" and _text(value) in constants:
            const_uses[(f.path, _text(value))].append((decision.model, value))
        if decision.keep:
            continue
        if value is None:
            decision.notes.append(f"No `model` property here: make sure {decision.model} is passed")
        elif value.type == "string":
            edits[f.path].append(Edit(value.start_byte, value.end_byte, quoted_like(value.text, decision.model)))
            comment = was_comment(f.source, value.end_byte, _unquote(_text(value)), b"//")
            if comment:
                edits[f.path].append(comment)
        elif not (value.type == "identifier" and _text(value) in constants):
            decision.notes.append(f"Model comes from `{_text(value)}`: set it to {decision.model}")
        if decision.extra_body:
            changed = _set_extra_body(f, call, decision.extra_body, site.sdk)
            if changed is None:
                decision.notes.append(f"Pass {decision.extra_body!r} in the request body by hand")
            else:
                edits[f.path] += changed
        for names, number, floor, label in (
            (("max_tokens", "max_completion_tokens"), decision.max_tokens, True, "allow at least {} output tokens"),
            (("temperature",), decision.temperature, False, "temperature {}"),
        ):
            if number is None or (floor and not number):
                continue
            changed = _set_number_property(f, call, names, number, floor)
            if changed is None:
                decision.notes.append(f"Tuned: {label.format(number)} (set by hand)")
            else:
                edits[f.path] += changed
        if decision.system_addendum and (note := _apply_ts_addendum(repo, f, call, site.sdk, decision, edits, text_edits, applied,
                                                                     kept_prompts)):
            decision.notes.append(note)
        if site.id in split_names:
            base = call.child_by_field_name("function")
            while base.type == "member_expression":
                base = base.child_by_field_name("object")
            edits[f.path].append(Edit(base.start_byte, base.end_byte, split_names[site.id].encode()))
    _port_ts_http(repo, http_sites, plan, http_calls, edits, const_uses, text_edits, applied, kept_prompts)

    # Shared model constants, as in the Python port: edited once only when nothing but calls moving to that model reads them.
    for (path, name), uses in const_uses.items():
        f = repo.file(path)
        constant = _top_level_strings(f)[name]
        targets = {model for model, _ in uses}
        if len(targets) == 1 and None not in targets and _ts_read_only_at(repo, f, name, [node for _, node in uses]):
            (target,) = targets
            edits[path].append(Edit(constant.start_byte, constant.end_byte, quoted_like(constant.text, target)))
            comment = was_comment(f.source, constant.end_byte, _unquote(_text(constant)), b"//")
            if comment:
                edits[path].append(comment)
        else:
            for model, node in uses:
                if model is not None:
                    edits[path].append(Edit(node.start_byte, node.end_byte, f'"{model}"'.encode()))

    runtime: dict[str, bytes] = {}
    if standins:  # the stand-in comes from OpenPort's runtime, copied in once
        path, text = _runtime(sorted(standins))
        runtime[path] = text
        for standin in standins:
            edits[standin].append(_import_edit(repo, repo.file(standin), path))

    # Settings added after the same last property each add its missing comma: keep one.
    output = {path: apply_edits(repo.file(path).source, list(dict.fromkeys(file_edits))) for path, file_edits in edits.items() if file_edits}
    return output | {path: apply_edits(raw, file_edits) for path, (raw, file_edits) in text_edits.items()} | runtime


def _apply_ts_addendum(repo: _TsRepo, f: _TsFile, call: Node, sdk: str, decision: SiteDecision, edits: dict, text_edits: dict,
                       applied: set, kept_prompts: set, properties: dict[str, Node] | None = None) -> str | None:
    """Write the tuned addendum into the call's system prompt. What to do by hand if it couldn't be placed."""
    addendum = decision.system_addendum
    resolved = _system_literal(repo, f, call, sdk, properties)
    if resolved:
        target, literal = resolved
        if (target.path, literal.start_byte) in kept_prompts:
            return f"Tuned: a call that stays uses this system prompt too, so give this call its own, with \"{addendum}\" appended"
        key = (target.path, literal.start_byte, addendum)
        if key in applied:
            return None
        edit = ts_literal_insertion(target.source, literal.start_byte, literal.end_byte, addendum)
        if edit:
            edits[target.path].append(edit)
            applied.add(key)
            return None
    placed = bool(decision.system_prompts)
    for prompt in decision.system_prompts:
        hit = find_prompt_file(repo.root, prompt)
        if hit is None:
            placed = False
            continue
        path, offset, raw = hit
        if ("file", path, offset) in kept_prompts:
            return f"Tuned: a call that stays uses the prompt in {path} too, so give this call its own, with \"{addendum}\" appended"
        if (path, offset, addendum) not in applied:
            text_edits.setdefault(path, (raw, []))[1].append(prompt_file_insertion(raw, offset, addendum))
            applied.add((path, offset, addendum))
    return None if placed else f"Tuned: append to the system prompt by hand: \"{addendum}\""
