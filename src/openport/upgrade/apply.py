"""Apply an upgrade to a copy of a repo: bump pinned versions, apply code edits, and diff the result.

The original repo is never modified. Edits are exact search-and-replace: each must match once, stay
inside the repo, and never touch tests, since the tests are what defines working. Files keep their
line endings.
"""

import difflib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..scan.findings import is_test_path

# .git comes along, so a proven upgrade can be committed and opened as a PR from the copy.
COPY_IGNORE = shutil.ignore_patterns(".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "runs")
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


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")  # keeps \r\n, which read_text would turn into \n


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")  # as is: no \n -> \r\n on Windows


def _margin(lines: list[str]) -> str:
    """The indentation every non-blank line shares."""
    return os.path.commonprefix([line[: len(line) - len(line.lstrip())] for line in lines if line.strip()])


def _dedent(lines: list[str], margin: str) -> list[str]:
    return [line[len(margin):] if line.startswith(margin) else line.lstrip() for line in lines]


def _replace_reindented(text: str, search: str, replace: str) -> str | None:
    """Replace `search` where it matches exactly one run of whole lines once indentation is set aside.

    Models often copy a snippet with the wrong indentation; the replacement is re-indented to the file's,
    keeping its indentation relative to the snippet. None when there's no single such match.
    """
    want = search.strip("\r\n").splitlines()
    if not want:
        return None
    key = [line.rstrip() for line in _dedent(want, _margin(want))]
    lines = text.splitlines(keepends=True)
    found = []
    for i in range(len(lines) - len(want) + 1):
        window = [line.rstrip("\r\n") for line in lines[i:i + len(want)]]
        margin = _margin(window)
        if [line.rstrip() for line in _dedent(window, margin)] == key:
            found.append((i, margin))
    if len(found) != 1:
        return None
    i, margin = found[0]
    block = lines[i:i + len(want)]
    newline = "\r\n" if block[0].endswith("\r\n") else "\n"
    new = [margin + line if line.strip() else "" for line in _dedent(replace.strip("\r\n").splitlines(), _margin(want))]
    ending = newline if new and block[-1].endswith("\n") else ""
    return "".join(lines[:i]) + newline.join(new) + ending + "".join(lines[i + len(want):])


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
        text = new = _read(path)
        for name, version in bumps.items():
            new, _ = bump_requirements(new, name, version)
        if new != text:
            _write(path, new)
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
            text = _read(path)
            newline = "\r\n" if "\r\n" in text else "\n"
            search, replace = (s.replace("\r\n", "\n").replace("\n", newline) for s in (edit.search, edit.replace))
            count = text.count(search) if search else 0
            new = text.replace(search, replace, 1) if count == 1 else _replace_reindented(text, search, replace) if count == 0 else None
            if new is None:
                rejected.append((edit, f"the text to replace appears {count} times, not once"))
                continue
            _write(path, new)
            applied.append(edit)
    return applied, rejected


def diff(root: Path, folder: Path) -> str:
    """A unified diff of every file that differs between the original repo and the upgraded copy."""
    chunks: list[str] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        relative = path.relative_to(folder)
        original = root / relative
        new = path.read_text(encoding="utf-8", errors="replace")
        old = original.read_text(encoding="utf-8", errors="replace") if original.is_file() else ""
        if old != new:
            name = relative.as_posix()
            chunks += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{name}", f"b/{name}")
    return "".join(chunks)
