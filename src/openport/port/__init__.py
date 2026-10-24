"""Port a repo onto Token Factory: copy it, rewrite the ported call sites, and report what changed.

The original is never modified. The ported copy gets an `openport.yaml` saying where each LLM call runs now.
"""

import difflib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import __version__, config
from ..runtime import openport_llm
from ..scan import ScanReport
from .plan import SiteDecision, build_plan, load_assignments
from .python_rewrite import port_python
from .ts_rewrite import port_typescript

COPY_IGNORE = shutil.ignore_patterns(
    "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".pytest_cache", ".mypy_cache"
)
SUMMARY_FILE = "openport.yaml"


@dataclass
class SiteOutcome:
    id: str
    file: str
    line: int
    sdk: str
    status: str  # "ported" or "kept"
    was: str | None
    now: str | None
    reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class PortResult:
    source: Path
    output: Path
    sites: list[SiteOutcome]
    changed: list[str]
    added: list[str]
    diff: str


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(function, target, _error):  # git object files are read-only on Windows
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)


def _prepare_output(source: Path, output: Path) -> None:
    if output.exists():
        managed = config.RUNS_DIR.resolve() / "ports" in output.parents
        if not managed and any(output.iterdir()):
            raise FileExistsError(f"{output} already exists and isn't empty")
        _remove_tree(output)
    shutil.copytree(source, output, ignore=COPY_IGNORE)


def _summary(outcomes: list[SiteOutcome]) -> bytes:
    sites = []
    for o in outcomes:
        entry = {"id": o.id, "file": o.file, "line": o.line, "status": o.status}
        if o.status == "ported":
            entry |= {"model": o.now, "was": o.was}
        else:
            entry |= {"model": o.was, "reason": o.reason}
        if o.notes:
            entry["check"] = o.notes
        sites.append(entry)
    header = "# Written by OpenPort: which model each LLM call uses after the port, and what it used before.\n"
    body = yaml.safe_dump({"openport": __version__, "call_sites": sites}, sort_keys=False, allow_unicode=True, width=120)
    return (header + body).encode()


def _diff(source: Path, output: Path, paths: list[str]) -> str:
    chunks = []
    for path in paths:
        before = (source / path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if (source / path).exists() else []
        after = (output / path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(before, after, f"a/{path}" if before else "/dev/null", f"b/{path}"))
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)


def port_repo(source: Path, report: ScanReport, plan: dict[str, SiteDecision], output: Path) -> PortResult:
    source, output = source.resolve(), output.resolve()
    _prepare_output(source, output)

    sites = report.portable()
    runtime_source = Path(openport_llm.__file__).read_bytes()
    files = port_python(output, [s for s in sites if s.language == "python"], plan, runtime_source)
    files |= port_typescript(output, [s for s in sites if s.language == "typescript"], plan)

    outcomes = [
        SiteOutcome(
            id=s.id,
            file=s.file,
            line=s.line,
            sdk=s.sdk,
            status="kept" if plan[s.id].keep else "ported",
            was=s.model or (f"set by `{s.model_expr}`" if s.model_expr else None),
            now=plan[s.id].model,
            reason=plan[s.id].reason,
            notes=plan[s.id].notes,
        )
        for s in sites
    ]
    files[SUMMARY_FILE] = _summary(outcomes)
    for path, content in files.items():
        (output / path).parent.mkdir(parents=True, exist_ok=True)
        (output / path).write_bytes(content)

    paths = sorted(files)
    added = [p for p in paths if not (source / p).exists()]
    changed = [p for p in paths if p not in added]
    return PortResult(source, output, outcomes, changed, added, _diff(source, output, paths))


__all__ = ["PortResult", "SiteDecision", "SiteOutcome", "build_plan", "load_assignments", "port_repo"]
