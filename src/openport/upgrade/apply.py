"""Apply an upgrade to a copy of a repo: bump pinned versions, apply code edits, and diff the result.

The original repo is never modified. Edits are exact search-and-replace: each must match once, stay
inside the repo, and never touch tests, since the tests are what defines working. Files keep their
line endings.
"""

import difflib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from ..scan.findings import is_test_path

SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "runs"}  # installs, caches, OpenPort's output
# Copies never carry .git: a container runs the repo's code in them, and it could plant hooks there.
# A PR is built from the original's .git instead (make_pr_folder).
COPY_IGNORE = shutil.ignore_patterns(".git", *SKIP_DIRS)
_PIN = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(\s*==\s*)([^\s;#]+)(.*)$")


@dataclass
class Edit:
    file: str
    search: str
    replace: str


def _remove_stubborn(function, target, error) -> None:
    """rmtree's fallback: make a read-only file (Git for Windows' objects) writable and retry, or remove a symlink
    a Linux container left on a Windows mount (uv's .venv/lib64), which Windows can't read but removes as a folder."""
    def writable_then_retry():
        os.chmod(target, stat.S_IWRITE)
        function(target)

    for attempt in (writable_then_retry, lambda: os.rmdir(target), lambda: os.unlink(target)):
        try:
            return attempt()
        except OSError:
            continue
    raise error


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_remove_stubborn)


def copy_repo(root: Path, out: Path) -> Path:
    if out.exists():
        remove_tree(out)
    shutil.copytree(root, out, ignore=COPY_IGNORE)
    return out


def _repo_files(root: Path):
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and ".git" not in relative.parts and not SKIP_DIRS & set(relative.parts) and path.suffix not in (".pyc", ".pyo"):
            yield relative


def changed_files(root: Path, folder: Path) -> list[str]:
    """The repo's files that differ in the copy. Only the original's files count: bumps and fixes edit files
    that exist, and anything new in the copy was made by an install or a test run (node_modules, .venv)."""
    return [r.as_posix() for r in _repo_files(root) if (folder / r).is_file() and (folder / r).read_bytes() != (root / r).read_bytes()]


def make_pr_folder(root: Path, folder: Path, files: list[str], dest: Path) -> Path:
    """A checkout to open the PR from: the original repo, its .git included, with only `files` taken from the
    upgraded copy. No container ever saw it, so nothing a repo's tests did can reach the commit or its hooks."""
    if dest.exists():
        remove_tree(dest)
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    for name in files:
        shutil.copy2(folder / name, dest / name)
    return dest


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")  # keeps \r\n, which read_text would turn into \n


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")  # as is: no \n -> \r\n on Windows


def _margin(lines: list[str]) -> str:
    """The indentation every non-blank line shares."""
    return os.path.commonprefix([line[: len(line) - len(line.lstrip())] for line in lines if line.strip()])


def _dedent(lines: list[str], margin: str) -> list[str]:
    return [line[len(margin):] if line.startswith(margin) else line.lstrip() for line in lines]


def _reindent(line: str, margin: str, target: str) -> str:
    """Move `line` from under `margin` to under `target`, keeping its indentation relative to the margin."""
    if not line.strip():
        return ""
    if line.startswith(margin):
        return target + line[len(margin):]
    indent = line[: len(line) - len(line.lstrip())]
    shallower = len(margin) - len(indent)  # left of the snippet, like an `except` after a `try` body
    return target[: max(len(target) - shallower, 0)] + line.lstrip()


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
    new = [_reindent(line, _margin(want), margin) for line in replace.strip("\r\n").splitlines()]
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
    """A unified diff of the repo's files that the upgrade changed in the copy (see changed_files)."""
    chunks: list[str] = []
    for name in changed_files(root, folder):
        old = (Path(root) / name).read_text(encoding="utf-8", errors="replace")
        new = (Path(folder) / name).read_text(encoding="utf-8", errors="replace")
        chunks += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{name}", f"b/{name}")
    return "".join(chunks)
