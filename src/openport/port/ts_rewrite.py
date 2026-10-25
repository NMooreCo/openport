"""Rewrite TypeScript/JavaScript OpenAI SDK code onto Token Factory.

The OpenAI Node SDK talks to Token Factory directly, so no runtime file is needed: the client gets
Token Factory's base URL and key, and each ported call gets its Nemotron model.
"""

import json
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
from .edits import Edit, apply_edits, before_newline, indentation, line_end, newline_style, quoted_like, was_comment
from .plan import SiteDecision
from .prompts import find_prompt_file, prompt_file_insertion, ts_literal_insertion

PROVIDER_ONLY = {"apiKey", "baseURL", "organization", "project"}
EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
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


def _system_literal(repo: _TsRepo, f: _TsFile, call: Node) -> tuple[_TsFile, Node] | None:
    """Follow a call's system message back to the string or template literal it's written in."""
    messages = _unwrap(_properties(call).get("messages"))
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


def _set_extra_body(f: _TsFile, call: Node, extra_body: dict) -> list[Edit] | None:
    """Write Token Factory-only request options (Nemotron's reasoning switch) into the call. The OpenAI Node SDK
    sends unknown properties as they are; its types don't know them, hence the @ts-expect-error."""
    args = call.child_by_field_name("arguments")
    obj = args.named_children[0] if args is not None and args.named_children else None
    if obj is None or obj.type != "object" or any(_object_value(obj, key) is not None for key in extra_body):
        return None
    edits: list[Edit] = []
    for key, value in extra_body.items():
        added = _add_property(f, obj, f"{key}: {_ts_value(value)}", "@ts-expect-error Token Factory option (Nemotron's reasoning switch), not in the OpenAI types")
        if added is None:
            return None
        edits += added
    return edits


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
    text_edits: dict[str, tuple[bytes, list[Edit]]] = {}
    applied: set = set()
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
            changed = _set_extra_body(f, call, decision.extra_body)
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
        if decision.system_addendum and not _apply_ts_addendum(repo, f, call, decision, edits, text_edits, applied):
            decision.notes.append(f"Tuned: append to the system prompt by hand: \"{decision.system_addendum}\"")
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

    # Settings added after the same last property each add its missing comma: keep one.
    output = {path: apply_edits(repo.file(path).source, list(dict.fromkeys(file_edits))) for path, file_edits in edits.items() if file_edits}
    return output | {path: apply_edits(raw, file_edits) for path, (raw, file_edits) in text_edits.items()}


def _apply_ts_addendum(repo: _TsRepo, f: _TsFile, call: Node, decision: SiteDecision, edits: dict, text_edits: dict, applied: set) -> bool:
    """Write the tuned addendum into the call's system prompt. False if it couldn't be placed."""
    addendum = decision.system_addendum
    resolved = _system_literal(repo, f, call)
    if resolved:
        target, literal = resolved
        key = (target.path, literal.start_byte, addendum)
        if key in applied:
            return True
        edit = ts_literal_insertion(target.source, literal.start_byte, literal.end_byte, addendum)
        if edit:
            edits[target.path].append(edit)
            applied.add(key)
            return True
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
