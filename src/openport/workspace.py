"""Fetch target repos into a local workspace.

Fetched code is only parsed by the scanner, never imported or run, until the user chooses to
run its evals.
"""

import re
import subprocess
from pathlib import Path

from . import config

_GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)?([A-Za-z0-9][\w.-]*)/([A-Za-z0-9_][\w.-]*?)(?:\.git)?/?$")


def is_remote(target: str) -> bool:
    return not Path(target).exists() and _GITHUB.match(target) is not None


def fetch_repo(target: str) -> tuple[Path, bool]:
    """Shallow-clone a GitHub repo (URL or owner/name) into runs/repos/, reusing an earlier clone.

    Returns the local path and whether it was freshly cloned.
    """
    match = _GITHUB.match(target)
    if match is None:
        raise ValueError(f"Not a GitHub repo: {target}")
    owner, name = match.groups()
    destination = config.RUNS_DIR / "repos" / f"{owner}__{name}"
    if destination.exists():
        return destination, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", f"https://github.com/{owner}/{name}.git", str(destination)],
        check=True,
    )
    return destination, True
