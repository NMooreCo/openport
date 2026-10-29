"""Fetch target repos into a local workspace.

Fetched code is only parsed by the scanner, never imported or run, until the user chooses to
run its evals.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from . import config

CLONE_TIMEOUT = int(os.getenv("OPENPORT_CLONE_TIMEOUT", "300"))  # seconds a fetch may take
# core.symlinks=false: a link in a fetched repo is checked out as a small file naming its target, so nothing that
# reads the clone (the scan, the port's diff, an upgrade's copy) can be led to a file outside it.
# core.longpaths: some repos have paths longer than Windows' 260 characters.
GIT_CONFIG = {"core.longpaths": "true", "core.symlinks": "false"}

_GITHUB = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?([A-Za-z0-9][\w.-]*)/([A-Za-z0-9_][\w.-]*?)(?:\.git)?/?(?:@([0-9a-f]{40}))?$"
)


# A folder on GitHub, as its page's URL: https://github.com/owner/name/tree/<branch>/<folder>.
_TREE = re.compile(r"^(?:https?://)?github\.com/([A-Za-z0-9][\w.-]*)/([A-Za-z0-9_][\w.-]*?)/tree/[^/]+/(.+?)/?$")


def is_remote(target: str) -> bool:
    return not Path(target).exists() and _GITHUB.match(target) is not None


def split_project(target: str) -> tuple[str, str]:
    """A monorepo project's GitHub URL as (the repo, the project's folder in it); anything else as (it, "").
    The folder is read from the default branch, which is what gets cloned."""
    match = _TREE.match(target.strip())
    return (f"{match[1]}/{match[2]}", match[3]) if match else (target, "")


def repo_size_mb(owner: str, name: str) -> float | None:
    """The repo's size as GitHub reports it (its whole history, so more than a shallow clone), or None if unknown."""
    try:
        response = httpx.get(f"https://api.github.com/repos/{owner}/{name}", timeout=10, headers={"Accept": "application/vnd.github+json"})
        return response.json()["size"] / 1024 if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def fetch_repo(target: str, max_mb: float | None = None) -> tuple[Path, bool]:
    """Shallow-clone a GitHub repo (URL or owner/name) into runs/repos/, reusing an earlier clone.

    `owner/name@<commit>` (the full hash) fetches exactly that commit, whatever the branch holds now:
    eval packs pin the commit whose code was reviewed. Returns the local path and whether it was
    freshly cloned. With `max_mb`, a repo GitHub says is bigger isn't fetched (a public server's disk).
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
    if max_mb is not None and (size := repo_size_mb(owner, name)) is not None and size > max_mb:
        raise ValueError(f"{owner}/{name} is {size:,.0f} MB on GitHub, over this server's {max_mb:,.0f} MB limit. Run OpenPort locally for it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")  # a failed fetch never looks like a clone
    shutil.rmtree(partial, ignore_errors=True)
    settings = [arg for key, value in GIT_CONFIG.items() for arg in ("--config", f"{key}={value}")]
    if commit is None:
        steps = [["git", "clone", "--depth", "1", "--quiet", *settings, url, str(partial)]]
    else:
        partial.mkdir()
        steps = [["git", "-C", str(partial), *args] for args in (
            ["init", "--quiet"],
            *(["config", key, value] for key, value in GIT_CONFIG.items()),
            ["remote", "add", "origin", url],  # `openport pr` finds the GitHub repo from origin, as in a clone
            ["fetch", "--depth", "1", "--quiet", "origin", commit],
            ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
        )]
    try:
        for step in steps:
            subprocess.run(step, check=True, timeout=CLONE_TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(f"Fetching {owner}/{name} took over {CLONE_TIMEOUT} seconds, so it was stopped.") from None
    partial.rename(destination)
    return destination, True
