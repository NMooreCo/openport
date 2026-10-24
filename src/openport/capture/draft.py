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


def runnable(finding: CallSiteFinding) -> tuple[bool, str]:
    """Whether the eval runner can import this call and swap its client. Returns (ok, reason-if-not)."""
    if finding.language != "python":
        return False, "TypeScript call sites can't be run by the eval harness yet"
    if not finding.module:
        return False, "no import path for the call site"
    if finding.function in ("<module>", "") or "." in finding.function:
        return False, f"not in a module-level function (found in {finding.function!r})"
    if finding.output not in RUNNABLE_OUTPUTS:
        return False, f"output type {finding.output!r} isn't evaluable"
    if client_attr_for(finding) is None:
        return False, f"client reached via {finding.receiver_kind} ({finding.receiver!r}); can't swap it at import"
    return True, ""


def param_names(root: Path, finding: CallSiteFinding) -> list[str]:
    """The parameter names of the call's enclosing function, read from source (not imported)."""
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


def draft_site(finding: CallSiteFinding, *, fields: list[str] | None = None) -> dict:
    """A call_sites entry for openport.evals.yaml, matching the hand-written spec's shape."""
    return {
        "id": finding.id,
        "title": humanize_title(finding),
        "file": finding.file,
        "module": finding.module,
        "function": finding.function,
        "client_attr": client_attr_for(finding) or "client",
        "sdk": finding.sdk,
        "original_model": finding.model or finding.model_expr or "unknown",
        "output": finding.output,
        "dataset": f"{finding.id}.jsonl",
        "grader": grader_for(finding.output, fields),
    }
