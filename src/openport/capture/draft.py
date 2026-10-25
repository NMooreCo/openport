"""Turn one scanned call site into a draft eval spec entry: a title, a bindable client, and a grader.

Everything here is derived from the scan (the code is parsed, never run), so a draft spec can be
produced without any API key. The datasets and, where needed, the grader details are filled in
afterwards from logs or generated cases.
"""

import ast
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
    if finding.receiver_kind in ("module_global", "imported"):
        return receiver
    if finding.receiver_kind == "factory":
        return receiver if receiver.endswith("()") else receiver + "()"
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
    if finding.function in ("<module>", "") or "." in finding.function:
        return False, f"not in a module-level function (found in {finding.function!r})"
    if finding.output not in RUNNABLE_OUTPUTS:
        return False, f"output type {finding.output!r} isn't evaluable"
    return True, ""


def _runnable_on_node(finding: CallSiteFinding, root: Path | None) -> tuple[bool, str]:
    """TypeScript/JavaScript runs under Node with its OpenAI or Anthropic SDK pointed at OpenPort's proxy (evals/node.py)."""
    if finding.output not in RUNNABLE_OUTPUTS:
        return False, f"output type {finding.output!r} isn't evaluable"
    if root is not None and not (root / "node_modules").is_dir():
        return False, "the project's dependencies aren't installed (run npm install), so its code can't run"
    if root is not None and ts_exported_params(root, finding) is None:
        return False, f"{finding.function!r} isn't an exported function the harness can call"
    return True, ""


def ts_exported_params(root: Path, finding: CallSiteFinding) -> list[str] | None:
    """The parameter names of the exported function the call is in, or None if it isn't one."""
    from ..scan.ts_scanner import _parser, _text, _walk

    try:
        source = (root / finding.file).read_bytes()
    except OSError:
        return None
    tree = _parser(finding.file.endswith((".tsx", ".jsx"))).parse(source)
    for node in _walk(tree.root_node):
        if node.type != "export_statement":
            continue
        for declared in _walk(node):
            name = declared.child_by_field_name("name")
            value = declared.child_by_field_name("value") if declared.type == "variable_declarator" else declared
            if _text(name) != finding.function or value is None or value.type not in (
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
        return ts_exported_params(root, finding) or []
    try:
        tree = ast.parse((root / finding.file).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    target = finding.function.split(".")[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target:
            args = node.args
            names = [p.arg for p in (args.posonlyargs + args.args + args.kwonlyargs)]
            return [n for n in names if n not in ("self", "cls")]
    return []


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
