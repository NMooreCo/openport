"""Apply an upgrade to a copy of a repo: bump pinned versions, apply code edits, and diff the result.

The original repo is never modified. Edits are exact search-and-replace: each must match once, stay
inside the repo without passing through a link, and never touch tests or what decides whether they pass
(test and CI configuration, the dependency files the bump owns), since the tests are what defines working.
Files keep their line endings.
"""

import difflib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config
from ..scan.findings import is_test_path

SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}  # installs and caches, wherever they are
# OpenPort's own output when it runs inside the repo (the weekly workflow): skipped at the repo's top only, so a source
# folder that happens to be called runs (Next.js's app/api/runs/) is copied like any other.
OUTPUT_DIR = "runs"
MARKER = ".openport-upgrade"  # in each folder an upgrade makes: a rerun replaces such a folder, and never one without it
_PIN = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(\s*==\s*)([^\s;#]+)(.*)$")

# What decides whether the tests pass rather than what they test: a fix may not change it to get them passing.
TEST_CONFIG_DIRS = {".github", ".circleci", "__snapshots__"}
TEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "conftest.py", "tests.py", "noxfile.py", ".coveragerc", ".gitlab-ci.yml"}
TEST_CONFIG = re.compile(r"(?:jest|vitest|playwright|cypress)\.config\.[^/]+|(?:jest|vitest)\.setup\.[^/]+|vitest\.workspace\.[^/]+"
                         r"|karma\.conf\.[^/]+|\.mocharc(?:\.[^/]+)?|setuptests\.[^/]+")
# What gets installed: the bump changes these, and a fix that did could pin the vulnerable release back (or run code
# at install, as .pnpmfile.cjs and Yarn's plugins do).
DEPENDENCY_DIRS = {".yarn"}
DEPENDENCY_FILES = {"package.json", "pyproject.toml", "uv.lock", "poetry.lock", "package-lock.json", "npm-shrinkwrap.json",
                    "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock", "bun.lock", "bun.lockb", "pipfile", "pipfile.lock",
                    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", "bunfig.toml"}
DEPENDENCY_FILE = re.compile(r"requirements[^/]*\.(?:txt|in)")


@dataclass
class Edit:
    file: str
    search: str
    replace: str


def _remove_stubborn(function, target, error) -> None:
    """rmtree's fallback: make a read-only file (Git for Windows' objects) writable and retry, or remove a symlink
    a Linux container left on a Windows mount (uv's .venv/lib64), which Windows can't read but removes as a folder."""
    def writable_then_retry():
        if not _link(Path(target)):  # chmod follows a link: its target could be any file on the machine
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


def _link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _parts(name: str) -> tuple[str, ...] | None:
    """A repo path's parts, or None if it isn't a plain relative path (absolute, with "..", a drive or a stream)."""
    pure = PurePosixPath(name.replace("\\", "/"))
    parts = tuple(part for part in pure.parts if part != ".")
    if pure.is_absolute() or not parts or any(part == ".." or ":" in part for part in parts):
        return None
    return parts


def through_link(base: Path, name: str) -> bool:
    """Whether base/name is a link, or is reached through one (a path that isn't plain counts as one)."""
    parts = _parts(name)
    return parts is None or any(_link(Path(base).joinpath(*parts[:i])) for i in range(1, len(parts) + 1))


def _ignore(top: Path, git: bool = False):
    """copytree's filter: installs and caches wherever they are, OpenPort's output at the top, and .git unless `git`.
    Copies a container works in never carry .git: the repo's code runs there, and could plant hooks in it."""
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in SKIP_DIRS or name == MARKER or (not git and name.lower() == ".git")}
        return skipped | ({OUTPUT_DIR} & set(names) if Path(directory) == Path(top) else set())

    return ignore


def _ours(folder: Path) -> bool:
    """Whether OpenPort made `folder`: it's under the runs folder, or an earlier upgrade marked it."""
    return (config.RUNS_DIR.resolve() in folder.resolve().parents or (folder / MARKER).is_file()
            or (folder / ".git" / MARKER).is_file())


def _clear(folder: Path) -> None:
    """Make way for a new folder: only an empty one, or one OpenPort made, is ever removed."""
    if _link(folder) or folder.exists() and not folder.is_dir():
        raise FileExistsError(f"{folder} exists and isn't a folder OpenPort made: choose another --out")
    if folder.exists():
        if any(folder.iterdir()) and not _ours(folder):
            raise FileExistsError(f"{folder} already exists, isn't empty, and wasn't made by OpenPort: choose another --out")
        remove_tree(folder)


def _mark(folder: Path) -> None:
    where = folder / ".git" if (folder / ".git").is_dir() else folder  # never among the files a PR commits
    (where / MARKER).write_text("Made by `openport upgrade`, which replaces this folder on its next run.\n", encoding="utf-8")


def copy_repo(root: Path, out: Path) -> Path:
    root, out = Path(root), Path(out)
    source, target = root.resolve(), out.resolve()
    if target == source or target in source.parents:  # removing it would remove the repo
        raise ValueError(f"{out} holds the repo itself: the upgrade's copy goes somewhere else")
    if source in target.parents and target.relative_to(source).parts[0] != OUTPUT_DIR:  # it would copy itself into itself
        raise ValueError(f"{out} is inside the repo: put the upgrade outside it, or under its {OUTPUT_DIR}/ folder")
    _clear(out)
    # Links stay links: followed here, one pointing out of the repo would copy that file into the folder the
    # repo's own tests run in, with the network. Inside the container a link can only reach the container.
    shutil.copytree(root, out, symlinks=True, ignore=_ignore(root))
    _mark(out)
    return out


def _repo_files(root: Path):
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == OUTPUT_DIR or ".git" in relative.parts or SKIP_DIRS & set(relative.parts):
            continue
        if path.is_file() and path.suffix not in (".pyc", ".pyo"):
            yield relative


def changed_files(root: Path, folder: Path, created: Iterable[str] = ()) -> list[str]:
    """The repo's files that differ in the copy, and `created`, the files a fix added. Nothing else new in the
    copy counts: it was made by an install or a test run (node_modules, .venv)."""
    changed = [r.as_posix() for r in _repo_files(root) if (folder / r).is_file() and (folder / r).read_bytes() != (root / r).read_bytes()]
    return changed + [name for name in dict.fromkeys(created) if (folder / name).is_file() and not (root / name).exists()]


def copy_problem(source: Path, destination: Path, name: str) -> str | None:
    """Why the file `name` (relative to both folders) can't be copied from `source` to `destination`, or None: only a
    regular file reached through no link comes, and only to a place reached through none, never into .git."""
    parts = _parts(name)
    if parts is None or any(part.lower() == ".git" for part in parts):
        return f"{name} isn't a file OpenPort copies"
    src, dst = Path(source).joinpath(*parts), Path(destination).joinpath(*parts)
    if through_link(source, name) or not src.is_file() or not src.resolve().is_relative_to(Path(source).resolve()):
        return f"{name} isn't a regular file there (it's a link, say), so it wasn't copied"
    if through_link(destination, name) or (dst.exists() and not dst.is_file()):
        return f"{name} would be written through a link, so it wasn't copied"
    return None


def copy_into(source: Path, destination: Path, name: str) -> None:
    """Copy the file `name` from `source` to `destination`, refusing what copy_problem refuses."""
    problem = copy_problem(source, destination, name)
    if problem:
        raise ValueError(problem)
    parts = _parts(name)
    target = Path(destination).joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)  # a file a fix created may be in a new folder
    shutil.copyfile(Path(source).joinpath(*parts), target)


def _checkout(root: Path, dest: Path) -> bool:
    """`dest` as the original's last commit, with a copy of its .git; False if there's none to check out (no .git, no
    commit yet, or a .git file: a worktree's or a submodule's, whose metadata is shared with the original)."""
    git = Path(root) / ".git"
    if not git.is_dir() or _link(git):
        return False
    from ..github import _safe  # git without the repo's own hooks or fsmonitor

    shutil.copytree(git, dest / ".git", symlinks=True)
    try:
        done = subprocess.run(_safe(["git", "reset", "--hard", "--quiet", "HEAD"]), cwd=dest, capture_output=True,
                              timeout=600).returncode == 0
    except (OSError, subprocess.SubprocessError):
        done = False
    if not done:
        remove_tree(dest)
    return done


def make_pr_folder(root: Path, folder: Path, files: list[str], dest: Path) -> Path:
    """A checkout to open the PR from: the original repo's last commit, its .git included, with only `files` taken
    from the upgraded copy. No container ever saw it, so nothing a repo's tests did can reach the commit or its hooks,
    and nothing uncommitted in the original (work in progress, untracked files) rides along into the PR."""
    root, dest = Path(root), Path(dest)
    _clear(dest)
    if not _checkout(root, dest):  # no commit to start from: the files as they are (a .git file stays behind)
        shutil.copytree(root, dest, symlinks=True, ignore=_ignore(root, git=(root / ".git").is_dir()))
    for name in files:
        copy_into(folder, dest, name)
    _mark(dest)
    return dest


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")  # keeps \r\n, which read_text would turn into \n


def _write(path: Path, text: str) -> None:
    if path.is_symlink():  # it could point out of the repo
        raise ValueError(f"{path.name} is a link, so OpenPort won't write through it")
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
        if path.is_symlink():  # a link's target may be anywhere: it isn't the repo's to bump
            continue
        text = new = _read(path)
        for name, version in bumps.items():
            new, _ = bump_requirements(new, name, version)
        if new != text:
            _write(path, new)
            changed.append(path.name)
    return changed


def hashed_pins(folder: Path) -> set[str]:
    """The packages (normalized names) a requirements file in `folder` pins with --hash. Bumping only the version
    would keep the old release's hashes, which pip then refuses."""
    names = set()
    for path in sorted(Path(folder).glob("requirements*.txt")):
        if path.is_symlink():
            continue
        text = re.sub(r"\\\r?\n", " ", path.read_text(encoding="utf-8", errors="replace"))  # a line ending in \ goes on
        for line in text.splitlines():
            match = _PIN.match(line)
            if match and "--hash" in match.group(6):
                names.add(_normalized(match.group(2)))
    return names


def edit_target(folder: Path, name: str) -> tuple[str, str]:
    """The file an edit names, as a plain path from `folder`, and why it can't be edited ("" if it can). The checks
    are on that path, so neither "src/../tests/x.py" nor a link into the tests gets past them."""
    parts = _parts(name)
    if parts is None:
        return name, "outside the repo"
    # Compared as Windows opens them, where "package.json." and "tests ./x.py" are package.json and tests/x.py.
    relative, lowered = "/".join(parts), [part.lower().rstrip(". ") for part in parts]
    if through_link(folder, relative):
        return relative, "it's a link, or inside one, so it could lead anywhere"
    if not Path(folder).joinpath(*parts).resolve().is_relative_to(Path(folder).resolve()):
        return relative, "outside the repo"
    if is_test_path(relative) or is_test_path("/".join(lowered)):
        return relative, "tests can't be changed to make them pass"
    if ".git" in lowered:
        return relative, "git's own files aren't the repo's code"
    if TEST_CONFIG_DIRS & set(lowered) or lowered[-1] in TEST_CONFIG_FILES or TEST_CONFIG.fullmatch(lowered[-1]):
        return relative, "test and CI configuration can't be changed to make the tests pass"
    if DEPENDENCY_DIRS & set(lowered) or lowered[-1] in DEPENDENCY_FILES or DEPENDENCY_FILE.fullmatch(lowered[-1]):
        return relative, "dependency files are the version bump's to change"
    if SKIP_DIRS & set(parts):
        return relative, "not the repo's own code (dependencies and build output)"
    return relative, ""


def apply_edits(folder: Path, edits: list[Edit]) -> tuple[list[Edit], list[tuple[Edit, str]]]:
    """Apply each edit that's allowed and matches. The applied ones come back naming their file as edit_target does."""
    applied: list[Edit] = []
    rejected: list[tuple[Edit, str]] = []
    for edit in edits:
        name, problem = edit_target(folder, edit.file)
        parts = name.split("/")
        path = Path(folder).joinpath(*parts)
        if problem:
            rejected.append((edit, problem))
        elif not edit.search:  # a new file the upgraded version needs
            blocker = next(("/".join(parts[:i]) for i in range(1, len(parts)) if Path(folder).joinpath(*parts[:i]).is_file()), None)
            if path.exists():
                rejected.append((edit, "it exists already: change it with a snippet to replace"))
            elif blocker:
                rejected.append((edit, f"{blocker} is a file, not a folder"))
            else:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _write(path, edit.replace)
                except (OSError, ValueError) as error:
                    rejected.append((edit, f"it can't be created: {error}"))
                    continue
                applied.append(Edit(name, edit.search, edit.replace))
        elif not path.is_file():
            rejected.append((edit, "no such file"))
        else:
            try:
                text = _read(path)
            except UnicodeDecodeError:
                rejected.append((edit, "not a UTF-8 text file"))
                continue
            newline = "\r\n" if "\r\n" in text else "\n"
            search, replace = (s.replace("\r\n", "\n").replace("\n", newline) for s in (edit.search, edit.replace))
            count = text.count(search) if search else 0
            new = text.replace(search, replace, 1) if count == 1 else _replace_reindented(text, search, replace) if count == 0 else None
            if new is None:
                rejected.append((edit, f"the text to replace appears {count} times, not once"))
                continue
            try:
                _write(path, new)
            except (OSError, ValueError) as error:
                rejected.append((edit, f"it can't be written: {error}"))
                continue
            applied.append(Edit(name, edit.search, edit.replace))
    return applied, rejected


def diff(root: Path, folder: Path, created: Iterable[str] = ()) -> str:
    """A unified diff of the repo's files that the upgrade changed in the copy, and the ones a fix created
    (see changed_files)."""
    chunks: list[str] = []
    for name in changed_files(root, folder, created):
        before = Path(root) / name
        old = before.read_text(encoding="utf-8", errors="replace") if before.is_file() else ""
        new = (Path(folder) / name).read_text(encoding="utf-8", errors="replace")
        chunks += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                       f"a/{name}" if before.is_file() else "/dev/null", f"b/{name}")
    return "".join(chunks)
