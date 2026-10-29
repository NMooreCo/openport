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
from ..check import DEFAULT_MIN_PASS_RATE, install_command, workflow
from ..evals.spec import SPEC_FILE
from ..runtime import openport_llm
from ..scan import SKIP_DIRS, ScanReport
from .plan import SiteDecision, build_plan, load_assignments
from .python_rewrite import port_python
from .ts_rewrite import port_typescript

COPY_IGNORE = shutil.ignore_patterns(
    "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".pytest_cache", ".mypy_cache"
)
SUMMARY_FILE = "openport.yaml"
WORKFLOW_FILE = ".github/workflows/openport.yml"


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
    # Links are copied as links, never followed: one pointing out of the repo can't pull a file into the copy.
    shutil.copytree(source, output, symlinks=True, ignore=COPY_IGNORE)


def _inside(root: Path, path: Path) -> bool:
    """Whether `path` stays in `root` once every link on the way is followed (it needn't exist yet)."""
    return path.resolve().is_relative_to(root.resolve())


def find_eval_spec(root: Path) -> str | None:
    for path in sorted(root.rglob(SPEC_FILE)):
        relative = path.relative_to(root)
        if not any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            return relative.as_posix()
    return None


def _summary(outcomes: list[SiteOutcome], gate: dict | None) -> bytes:
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
    document = {"openport": __version__}
    if gate:
        document["gate"] = gate  # read by `openport check` and the GitHub Action
    document["call_sites"] = sites
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=120)
    return (header + body).encode()


def _diff(source: Path, output: Path, paths: list[str]) -> str:
    chunks = []
    for path in paths:
        original = source / path
        # A link's target is never read into the diff: the diff is shown and saved, and it could name any file.
        exists = original.is_file() and _inside(source, original)
        before = original.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if exists else []
        after = (output / path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(before, after, f"a/{path}" if before else "/dev/null", f"b/{path}"))
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)


def port_repo(
    source: Path, report: ScanReport, plan: dict[str, SiteDecision], output: Path, min_pass_rate: float = DEFAULT_MIN_PASS_RATE
) -> PortResult:
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
    # The quality gate: if the repo has an eval spec and Python calls were ported, CI re-runs it on every PR.
    gate = None
    evals = find_eval_spec(output)
    if evals and any(o.status == "ported" and s.language == "python" for o, s in zip(outcomes, sites)):
        gate = {"evals": evals, "min_pass_rate": min_pass_rate}
        if not (output / WORKFLOW_FILE).exists():
            files[WORKFLOW_FILE] = workflow(install_command(output), config.openport_source()).encode()
    files[SUMMARY_FILE] = _summary(outcomes, gate)
    for path, content in files.items():
        target = output / path
        if target.is_symlink() and path in (SUMMARY_FILE, WORKFLOW_FILE):
            target.unlink()  # OpenPort's own files: a link in their place is replaced, not written through
        if not _inside(output, target):  # checked before any folder is made, so none is made through a link either
            raise ValueError(f"{path} is reached through a link out of the repo, so OpenPort won't write it")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    paths = sorted(files)
    added = [p for p in paths if not (source / p).exists()]
    changed = [p for p in paths if p not in added]
    return PortResult(source, output, outcomes, changed, added, _diff(source, output, paths))


__all__ = ["PortResult", "SiteDecision", "SiteOutcome", "build_plan", "load_assignments", "port_repo"]
