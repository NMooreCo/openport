"""Apply an upgrade to a copy of a repo: bump pinned versions, apply code edits, and diff the result.

The original repo is never modified. Edits are exact search-and-replace: each must match once, stay
inside the repo, and never touch tests, since the tests are what defines working.
"""

import difflib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..scan.findings import is_test_path

COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "runs")
_PIN = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(\s*==\s*)([^\s;#]+)(.*)$")


@dataclass
class Edit:
    file: str
    search: str
    replace: str


def copy_repo(root: Path, out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(root, out, ignore=COPY_IGNORE)
    return out


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def bump_requirements(text: str, name: str, version: str) -> tuple[str, bool]:
    """Change the exact pin for `name` (any spelling), keeping extras, markers, comments and line endings."""
    lines, changed = [], False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _PIN.match(body)
        if match and _normalized(match.group(2)) == _normalized(name):
            line = f"{match.group(1)}{match.group(2)}{match.group(3) or ''}{match.group(4)}{version}{match.group(6)}{line[len(body):]}"
            changed = True
        lines.append(line)
    return "".join(lines), changed


def apply_bumps(folder: Path, bumps: dict[str, str]) -> list[str]:
    """Bump each package to its version in the folder's requirements files. Returns the files changed."""
    changed = []
    for path in sorted(folder.glob("requirements*.txt")):
        text = new = path.read_text(encoding="utf-8")
        for name, version in bumps.items():
            new, _ = bump_requirements(new, name, version)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.name)
    return changed


def apply_edits(folder: Path, edits: list[Edit]) -> tuple[list[Edit], list[tuple[Edit, str]]]:
    applied: list[Edit] = []
    rejected: list[tuple[Edit, str]] = []
    root = folder.resolve()
    for edit in edits:
        path = (folder / edit.file).resolve()
        if root not in path.parents:
            rejected.append((edit, "outside the repo"))
        elif is_test_path(Path(edit.file).as_posix()):
            rejected.append((edit, "tests can't be changed to make them pass"))
        elif not path.is_file():
            rejected.append((edit, "no such file"))
        else:
            text = path.read_text(encoding="utf-8")
            count = text.count(edit.search) if edit.search else 0
            if count != 1:
                rejected.append((edit, f"the text to replace appears {count} times, not once"))
                continue
            path.write_text(text.replace(edit.search, edit.replace, 1), encoding="utf-8")
            applied.append(edit)
    return applied, rejected


def diff(root: Path, folder: Path) -> str:
    """A unified diff of every file that differs between the original repo and the upgraded copy."""
    chunks: list[str] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        relative = path.relative_to(folder)
        original = root / relative
        new = path.read_text(encoding="utf-8", errors="replace")
        old = original.read_text(encoding="utf-8", errors="replace") if original.is_file() else ""
        if old != new:
            name = relative.as_posix()
            chunks += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{name}", f"b/{name}")
    return "".join(chunks)
