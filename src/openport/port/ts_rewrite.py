"""Rewrite TypeScript/JavaScript OpenAI SDK code onto Token Factory.

The OpenAI Node SDK talks to Token Factory directly, so no runtime file is needed: the client gets
Token Factory's base URL and key, and each ported call gets its Nemotron model.
"""

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from ..runtime.openport_llm import TOKEN_FACTORY_BASE_URL
from ..scan.findings import CALL_SUFFIXES, CallSiteFinding
from ..scan.ts_scanner import _parser, _text, _unquote, _walk
from .clients import decide_client_actions
from .edits import Edit, apply_edits, indentation, line_end, newline_style, quoted_like, was_comment
from .plan import SiteDecision

PROVIDER_ONLY = {"apiKey", "baseURL", "organization", "project"}
EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
# `?? ""` matters: with apiKey undefined, the SDK would fall back to OPENAI_API_KEY and send it to Nebius.
CONNECTION = [f'baseURL: "{TOKEN_FACTORY_BASE_URL}"', 'apiKey: process.env.NEBIUS_API_KEY ?? ""']


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
        found = []
        for node in _walk(f.root):
            if node.type != "variable_declarator":
                continue
            value = node.child_by_field_name("value")
            if value is None or value.type != "new_expression":
                continue
            ctor = _text(value.child_by_field_name("constructor")).split(".")[-1]
            if ctor not in ("OpenAI", "AzureOpenAI"):
                continue
            statement = node.parent
            top = statement.parent.type == "program" or (
                statement.parent.type == "export_statement" and statement.parent.parent.type == "program"
            )
            found.append(_TsBinding(f.path, _text(node.child_by_field_name("name")), ctor, value, statement, top))
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

    def link(self, site: CallSiteFinding) -> list[_TsBinding]:
        f = self.file(site.file)
        if f is None:
            return []
        receiver = re.sub(r"(?:\.beta)+$", "", site.receiver)
        local = [b for b in self.bindings(f) if b.var == receiver]
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
                    return [b for b in self.bindings(target) if b.var == _text(name)] if target else []
        return []


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


def _arguments_text(f: _TsFile, binding: _TsBinding) -> bytes:
    """New constructor arguments: Token Factory's URL and key, plus every other option the original passed."""
    args = binding.new_expr.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    kept: list[str] = []
    if first is not None and first.type == "object":
        for child in first.named_children:
            if child.type == "pair" and _unquote(_text(child.child_by_field_name("key"))) in PROVIDER_ONLY:
                continue
            kept.append(_text(child) if child.type == "comment" else _text(child) + ",")
    elif first is not None:  # options passed in as a variable
        kept.append(f"...{_text(first)},")

    entries = [c + "," for c in CONNECTION] + kept
    if first is not None and "\n" in _text(first):
        base = indentation(f.source, binding.statement.start_byte).decode()
        inner = base + "  "
        nl = newline_style(f.source).decode()
        return ("({" + nl + "".join(f"{inner}{e}{nl}" for e in entries) + base + "})").encode()
    return ("({ " + " ".join(entries).rstrip(",") + " })").encode()


def port_typescript(root: Path, sites: list[CallSiteFinding], plan: dict[str, SiteDecision]) -> dict[str, bytes]:
    """Rewrite the TypeScript/JavaScript files under `root` for the ported OpenAI sites in `plan`."""
    repo = _TsRepo(root)
    links = {site.id: repo.link(site) for site in sites}
    for site in sites:
        azure = [b for b in links[site.id] if b.ctor != "OpenAI"]
        if azure and not plan[site.id].keep:
            plan[site.id].keep_because(f"built with {azure[0].ctor}: port by hand")
            links[site.id] = []
    actions = decide_client_actions(
        sites,
        plan,
        links,
        key=lambda b: (b.file, b.new_expr.start_byte),
        splittable=lambda b, ported: b.top_level and all(s.file == b.file for s in ported),
    )

    edits: dict[str, list[Edit]] = defaultdict(list)
    split_names: dict[str, str] = {}
    for action in actions.values():
        binding: _TsBinding = action.binding
        f = repo.file(binding.file)
        arguments = _arguments_text(f, binding)
        if action.action == "replace":
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
            line = indent + f"{keyword} {new_var} = new OpenAI".encode() + arguments + b";" + nl
            position = line_end(f.source, binding.statement.end_byte)
            edits[f.path].append(Edit(position, position, line))
            for site in action.ported:
                split_names[site.id] = new_var

    const_uses: dict[tuple[str, str], list[tuple[str | None, Node]]] = defaultdict(list)
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
            decision.notes.append(f"Pass {decision.extra_body!r} in the request body by hand")
        if site.id in split_names:
            base = call.child_by_field_name("function")
            while base.type == "member_expression":
                base = base.child_by_field_name("object")
            edits[f.path].append(Edit(base.start_byte, base.end_byte, split_names[site.id].encode()))

    for (path, name), uses in const_uses.items():
        f = repo.file(path)
        constant = _top_level_strings(f)[name]
        targets = {model for model, _ in uses}
        if len(targets) == 1 and None not in targets:
            (target,) = targets
            edits[path].append(Edit(constant.start_byte, constant.end_byte, quoted_like(constant.text, target)))
            comment = was_comment(f.source, constant.end_byte, _unquote(_text(constant)), b"//")
            if comment:
                edits[path].append(comment)
        else:
            for model, node in uses:
                if model is not None:
                    edits[path].append(Edit(node.start_byte, node.end_byte, f'"{model}"'.encode()))

    return {path: apply_edits(repo.file(path).source, file_edits) for path, file_edits in edits.items() if file_edits}
