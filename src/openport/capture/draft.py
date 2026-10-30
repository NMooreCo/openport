"""Turn one scanned call site into a draft eval spec entry: a title, a bindable client, and a grader.

Everything here is derived from the scan (the code is parsed, never run), so a draft spec can be
produced without any API key. The datasets and, where needed, the grader details are filled in
afterwards from logs or generated cases.
"""

import ast
import re
from pathlib import Path

from ..scan.findings import CallSiteFinding

# Output shapes the eval runner knows how to drive and grade.
RUNNABLE_OUTPUTS = {"text", "json_in_text", "json_object", "json_schema", "tools"}

GENERIC_RUBRIC = (
    "You are reviewing the output of an LLM call that moved from a closed model to NVIDIA Nemotron. "
    "Score, from 1 to 5, how well the output does the job for the given input.\n"
    "5: equivalent in substance, format and tone; a reviewer couldn't tell it from the original.\n"
    "4: good, needs at most one small edit.\n"
    "3: usable only with real edits.\n"
    "2: wrong focus, format or facts.\n"
    "1: unusable or harmful."
)


def humanize_title(finding: CallSiteFinding) -> str:
    name = finding.function.split(".")[-1]
    if name in ("<module>", ""):
        name = Path(finding.file).stem
    return name.replace("_", " ").replace("-", " ").strip().capitalize() or finding.id


def client_attr_for(finding: CallSiteFinding) -> str | None:
    """How the eval runner reaches the call's client, or None when it can't be swapped at import time."""
    receiver = finding.receiver or "client"
    name = receiver.removesuffix("()")
    if not re.fullmatch(r"[A-Za-z_]\w*", name):  # openai.OpenAI(), make("x")(): not an attribute the runner can set
        return None
    if finding.receiver_kind in ("module_global", "imported"):
        return receiver
    if finding.receiver_kind == "factory":
        return name + "()"
    return None  # self.client, a local, or an expression: nothing to set on the module


def grader_for(output: str, fields: list[str] | None) -> dict:
    """A reasonable starting grader for an output shape. Free text gets an Ultra judge; the rest, field checks."""
    if output == "text":
        return {"type": "text_rules", "judge": {"min_score": 4, "rubric": GENERIC_RUBRIC}}
    return {"type": "fields", "fields": list(fields or [])}


def runnable(finding: CallSiteFinding, root: Path | None = None) -> tuple[bool, str]:
    """Whether the eval runner can call this function and route its SDK calls. Returns (ok, reason-if-not).
    Where the client comes from doesn't matter: the runner intercepts the SDK itself (evals/intercept.py)."""
    if finding.sdk == "http":  # its requests are redirected to OpenPort's stand-in for the API
        if finding.api not in ("chat.completions", "messages"):
            return False, f"a plain HTTP call to the {finding.api} API, which isn't evaluable"
        if finding.language != "python" and finding.receiver not in ("fetch", "$fetch", "ofetch"):
            return False, f"a plain HTTP call through {finding.receiver}: the harness redirects fetch, not that"
    if finding.language != "python":
        return _runnable_on_node(finding, root)
    if not finding.module:
        return False, "no import path for the call site"
    if finding.function in ("<module>", ""):
        return False, "not in a function (found at the module's top level)"
    if "." in finding.function:
        ok, why = _method_buildable(root, finding)
        if not ok:
            return False, why
    if finding.output not in RUNNABLE_OUTPUTS:
        return False, f"output type {finding.output!r} isn't evaluable"
    return True, ""


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_PLAIN_BASES = {"object", "ABC", "Protocol", "Generic"}
_MODEL_BASES = {"BaseModel", "BaseSettings"}  # pydantic: built from its fields, like a dataclass


def _method(root: Path, finding: CallSiteFinding) -> tuple[ast.ClassDef, ast.AST, dict[str, ast.ClassDef]] | None:
    """The module-level class and method a call is in (`Assistant.reply`), and the module's classes."""
    owner, _, name = finding.function.rpartition(".")
    try:
        tree = ast.parse((root / finding.file).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    cls = classes.get(owner)
    method = next((n for n in cls.body if isinstance(n, _FUNCTIONS) and n.name == name), None) if cls else None
    return (cls, method, classes) if method else None


def _name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""


def _required(cls: ast.ClassDef, classes: dict[str, ast.ClassDef], depth: int = 0) -> list[str] | str:
    """The arguments building `cls` needs, or why OpenPort can't tell (a base class it can't see)."""
    init = next((n for n in cls.body if isinstance(n, _FUNCTIONS) and n.name == "__init__"), None)
    if init is not None:
        args = init.args
        positional = [*args.posonlyargs, *args.args][1:]  # after self
        required = positional[: len(positional) - len(args.defaults)] if args.defaults else positional
        return [a.arg for a in required] + [a.arg for a, default in zip(args.kwonlyargs, args.kw_defaults) if default is None]
    bases = [_name(b.value if isinstance(b, ast.Subscript) else b) for b in cls.bases]
    if any(_name(d) == "dataclass" for d in cls.decorator_list) or _MODEL_BASES & set(bases):
        return [n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and n.value is None and isinstance(n.target, ast.Name)
                and "ClassVar" not in ast.unparse(n.annotation)]
    for base in (b for b in bases if b not in _PLAIN_BASES):
        if base not in classes or depth > 5:
            return f"built from {base}, which OpenPort can't see into"
        return _required(classes[base], classes, depth + 1)
    return []


def _method_buildable(root: Path | None, finding: CallSiteFinding) -> tuple[bool, str]:
    """A call in a class's method can be tested on an instance built with no arguments, or straight from the
    class when the method is static or a classmethod (see evals/entry.py)."""
    found = _method(root, finding) if root is not None else None
    if found is None:
        return False, f"in {finding.function!r}, not a module-level function or a method of a module-level class"
    cls, method, classes = found
    if any(_name(d) in ("staticmethod", "classmethod") for d in method.decorator_list):
        return True, ""
    required = _required(cls, classes)
    if isinstance(required, str):
        return False, f"a method of {cls.name}, {required}: give the arguments to build it as `init:` in the spec"
    if required:
        return False, f"a method of {cls.name}, which takes {', '.join(required)} to build: give them as `init:` in the spec"
    return True, ""


def _runnable_on_node(finding: CallSiteFinding, root: Path | None) -> tuple[bool, str]:
    """TypeScript/JavaScript runs under Node with its OpenAI or Anthropic SDK pointed at OpenPort's proxy (evals/node.py)."""
    if finding.output not in RUNNABLE_OUTPUTS:
        return False, f"output type {finding.output!r} isn't evaluable"
    if root is not None and not (root / "node_modules").is_dir():
        return False, "the project's dependencies aren't installed (run npm install), so its code can't run"
    if root is not None and ts_entry_params(root, finding) is None:
        return False, f"{finding.function!r} isn't at the top of its module, where the harness can call it"
    if root is not None and "." in finding.function:
        cls, method = _ts_method(root, finding)
        required = [] if any(c.type == "static" for c in method.children) else _ts_required(cls)
        owner = finding.function.split(".")[0]
        if isinstance(required, str):
            return False, f"a method of {owner}, {required}: give the arguments to build it as `init:` in the spec"
        if required:
            return False, f"a method of {owner}, which takes {', '.join(required)} to build: give them as `init:` in the spec"
    return True, ""


def _ts_tree(root: Path, finding: CallSiteFinding):
    from ..scan.ts_scanner import _parser

    try:
        return _parser(finding.file.endswith((".tsx", ".jsx"))).parse((root / finding.file).read_bytes())
    except OSError:
        return None


def _ts_top_level(tree):
    """A module's top-level declarations, exported or not: the harness can call a function or class that isn't
    exported too (the Node runner exports it as the module loads)."""
    for statement in tree.root_node.named_children if tree else []:
        yield statement.named_children[0] if statement.type == "export_statement" and statement.named_children else statement


def _ts_method(root: Path, finding: CallSiteFinding):
    """The top-level class and method a call is in (`Assistant.reply`): (class, method) nodes, or (None, None)."""
    from ..scan.ts_scanner import _text

    owner, _, name = finding.function.partition(".")
    for cls in _ts_top_level(_ts_tree(root, finding)) if "." not in name else []:
        if cls.type not in ("class_declaration", "abstract_class_declaration") or _text(cls.child_by_field_name("name")) != owner:
            continue
        body = cls.child_by_field_name("body")
        method = next((m for m in body.named_children if m.type == "method_definition" and _text(m.child_by_field_name("name")) == name), None)
        if method is not None:
            return cls, method
    return None, None


def _ts_required(cls) -> list[str] | str:
    """The arguments building an exported class needs: its constructor's parameters without defaults."""
    from ..scan.ts_scanner import _text

    body = cls.child_by_field_name("body")
    constructor = next((m for m in body.named_children if m.type == "method_definition" and _text(m.child_by_field_name("name")) == "constructor"), None)
    if constructor is None:
        heritage = next((c for c in cls.named_children if c.type == "class_heritage"), None)
        return "built like the class it extends, which OpenPort doesn't look into" if heritage is not None else []
    params = constructor.child_by_field_name("parameters")
    return [_text(p.child_by_field_name("pattern")) for p in (params.named_children if params else [])
            if p.type == "required_parameter" and p.child_by_field_name("value") is None
            and getattr(p.child_by_field_name("pattern"), "type", "") != "rest_pattern"]


def ts_entry_params(root: Path, finding: CallSiteFinding) -> list[str] | None:
    """The parameter names of the function the call is in, at the top of its module (exported or not), or of its
    top-level class's method; None when the harness can't call it (say, a function inside another)."""
    from ..scan.ts_scanner import _text

    if "." in finding.function:
        _, method = _ts_method(root, finding)
        params = method.child_by_field_name("parameters") if method is not None else None
        if method is None:
            return None
        names = [_text(p.child_by_field_name("pattern") or p) for p in (params.named_children if params else [])]
        return [n for n in names if n.isidentifier()]
    for declaration in _ts_top_level(_ts_tree(root, finding)):
        declared = [declaration] if declaration.type in ("function_declaration", "generator_function_declaration") else [
            d for d in declaration.named_children if d.type == "variable_declarator"] if declaration.type in ("lexical_declaration", "variable_declaration") else []
        for node in declared:
            value = node.child_by_field_name("value") if node.type == "variable_declarator" else node
            if _text(node.child_by_field_name("name")) != finding.function or value is None or value.type not in (
                "function_declaration", "generator_function_declaration", "function_expression", "arrow_function", "function",
            ):
                continue
            params = value.child_by_field_name("parameters")
            single = value.child_by_field_name("parameter")  # `x => ...`
            names = [_text(p.child_by_field_name("pattern") or p) for p in (params.named_children if params else [])]
            return [n for n in names if n.isidentifier()] or ([_text(single)] if single else [])
    return None


def param_names(root: Path, finding: CallSiteFinding) -> list[str]:
    """The parameter names of the call's enclosing function, read from source (not imported)."""
    if finding.language != "python":
        return ts_entry_params(root, finding) or []
    found = _method(root, finding) if "." in finding.function else None
    node = found[1] if found else None
    if node is None:
        try:
            tree = ast.parse((root / finding.file).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return []
        target = finding.function.split(".")[-1]
        node = next((n for n in ast.walk(tree) if isinstance(n, _FUNCTIONS) and n.name == target), None)
    if node is None:
        return []
    args = node.args
    names = [p.arg for p in (args.posonlyargs + args.args + args.kwonlyargs)]
    return [n for n in names if n not in ("self", "cls")]


def draft_site(finding: CallSiteFinding, *, fields: list[str] | None = None, args: list[str] | None = None) -> dict:
    """A call_sites entry for openport.evals.yaml, matching the hand-written spec's shape."""
    if finding.language != "python":  # runs under Node: the file's export, called with the input's fields in order
        return {
            "id": finding.id,
            "title": humanize_title(finding),
            "file": finding.file,
            "function": finding.function,
            "args": list(args or []),
            "sdk": finding.sdk,
            "original_model": finding.model or finding.model_expr or "unknown",
            "output": finding.output,
            "dataset": f"{finding.id}.jsonl",
            "grader": grader_for(finding.output, fields),
        }
    attr = client_attr_for(finding)
    return {
        "id": finding.id,
        "title": humanize_title(finding),
        "file": finding.file,
        "module": finding.module,
        "function": finding.function,
        **({"client_attr": attr} if attr else {"intercept": True}),  # no attribute to swap: route the SDK's calls
        "sdk": finding.sdk,
        "original_model": finding.model or finding.model_expr or "unknown",
        "output": finding.output,
        "dataset": f"{finding.id}.jsonl",
        "grader": grader_for(finding.output, fields),
    }
