"""Scan a repo for OpenAI and Anthropic SDK calls, and say what each one needs to move to Token Factory."""

import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .findings import CallSiteFinding, is_test_path
from .python_scanner import scan_python
from .ts_scanner import scan_typescript

SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "out",
    "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "coverage", ".turbo", ".vercel",
}
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
MAX_FILE_BYTES = 500_000  # skip bundles and minified files


@dataclass
class ScanReport:
    root: str
    files_scanned: int
    findings: list[CallSiteFinding]
    skipped: list[str] = field(default_factory=list)  # files that couldn't be parsed

    def portable(self, include_tests: bool = False) -> list[CallSiteFinding]:
        return [f for f in self.findings if include_tests or not f.in_tests]

    def summary(self, include_tests: bool = False) -> dict:
        sites = self.portable(include_tests)
        return {
            "call_sites": len(sites),
            "by_sdk": dict(Counter(f.sdk for f in sites)),
            "by_portability": dict(Counter(f.portability for f in sites)),
            "models": dict(Counter(f.model or f.model_expr or "?" for f in sites)),
            "in_tests": sum(f.in_tests for f in self.findings),
        }

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "skipped": self.skipped,
            "summary": self.summary(),
            "findings": [{**asdict(f), "portability": f.portability, "notes": f.notes} for f in self.findings],
        }


def _source_files(root: Path):
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py") or (name.endswith(TS_SUFFIXES) and not name.endswith(".d.ts") and ".min." not in name):
                yield Path(directory) / name


def _assign_ids(findings: list[CallSiteFinding]) -> None:
    seen: Counter = Counter()
    for finding in findings:
        stem = Path(finding.file).stem
        function = finding.function.split(".")[-1]
        base = stem if function in ("<module>", stem) else f"{stem}.{function}"
        seen[base] += 1
        finding.id = base if seen[base] == 1 else f"{base}-{seen[base]}"


def scan_repo(root: Path) -> ScanReport:
    root = root.resolve()
    findings: list[CallSiteFinding] = []
    skipped: list[str] = []
    files = 0
    for path in sorted(_source_files(root)):
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        relative = path.relative_to(root).as_posix()
        in_tests = is_test_path(relative)
        files += 1
        try:
            if path.suffix == ".py":
                findings += scan_python(path.read_text(encoding="utf-8", errors="replace"), relative, in_tests)
            else:
                findings += scan_typescript(path.read_bytes(), relative, in_tests)
        except (SyntaxError, ValueError) as error:
            skipped.append(f"{relative}: {type(error).__name__}")
    _assign_ids(findings)
    return ScanReport(root=str(root), files_scanned=files, findings=findings, skipped=skipped)


__all__ = ["CallSiteFinding", "ScanReport", "scan_repo"]
