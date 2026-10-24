"""Fetch target repos into a local workspace.

Fetched code is only parsed by the scanner, never imported or run, until the user chooses to
run its evals.
"""

import re
import shutil
import subprocess
from pathlib import Path

from . import config

_GITHUB = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?([A-Za-z0-9][\w.-]*)/([A-Za-z0-9_][\w.-]*?)(?:\.git)?/?(?:@([0-9a-f]{40}))?$"
)


def is_remote(target: str) -> bool:
    return not Path(target).exists() and _GITHUB.match(target) is not None


def fetch_repo(target: str) -> tuple[Path, bool]:
    """Shallow-clone a GitHub repo (URL or owner/name) into runs/repos/, reusing an earlier clone.

    `owner/name@<commit>` (the full hash) fetches exactly that commit, whatever the branch holds now:
    eval packs pin the commit whose code was reviewed. Returns the local path and whether it was
    freshly cloned.
    """
    match = _GITHUB.match(target)
    if match is None:
        raise ValueError(f"Not a GitHub repo: {target}")
    owner, name, commit = match.groups()
    url = f"https://github.com/{owner}/{name}.git"
    repos = config.RUNS_DIR / "repos"
    destination = (repos / commit[:12] if commit else repos) / f"{owner}__{name}"  # same folder name either way
    if destination.exists():
        return destination, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")  # a failed fetch never looks like a clone
    shutil.rmtree(partial, ignore_errors=True)
    # core.longpaths: some repos have paths longer than Windows' 260 characters.
    if commit is None:
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", "--config", "core.longpaths=true", url, str(partial)], check=True)
    else:
        partial.mkdir()
        for args in (
            ["init", "--quiet"],
            ["config", "core.longpaths", "true"],
            ["remote", "add", "origin", url],  # `openport pr` finds the GitHub repo from origin, as in a clone
            ["fetch", "--depth", "1", "--quiet", "origin", commit],
            ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ):
            subprocess.run(["git", "-C", str(partial), *args], check=True)
    partial.rename(destination)
    return destination, True
