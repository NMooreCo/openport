"""Open a port or an upgrade as a PR: commit on a branch, push to your fork, and open the PR there.

The PR is opened inside your fork (fork branch -> fork default branch), so the upstream
maintainers aren't notified. Pass `upstream=True` to propose it to the original repo instead.
"""

import functools
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

BRANCH = "openport/nemotron"
UPGRADE_BRANCH = "openport/security-upgrades"


@functools.cache
def _no_hooks() -> str:
    return tempfile.mkdtemp(prefix="openport-no-hooks-")


def _safe(command: list[str]) -> list[str]:
    """Git with no hooks and no fsmonitor: a repo's own config can't make OpenPort run its commands."""
    if command[:1] != ["git"]:
        return command
    return ["git", "-c", f"core.hooksPath={_no_hooks()}", "-c", "core.fsmonitor=false", *command[1:]]


@dataclass
class PrPlan:
    repo: str  # owner/name of the original
    fork: str  # owner/name the branch is pushed to
    base_repo: str  # where the PR is opened
    commands: list[list[str]]


def _run(command: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(_safe(command), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def origin_repo(ported: Path) -> str:
    try:
        url = _run(["git", "remote", "get-url", "origin"], cwd=ported)
    except subprocess.CalledProcessError:
        raise ValueError("it has no origin remote; push the repo to GitHub first, then run OpenPort on the clone") from None
    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    if match is None:
        raise ValueError(f"origin isn't a GitHub repo: {url}")
    return f"{match[1]}/{match[2]}"


def plan_pr(ported: Path, title: str, body_file: Path, login: str, upstream: bool = False, branch: str = BRANCH) -> PrPlan:
    if not (ported / ".git").exists():
        raise ValueError(f"{ported} isn't a git repository; push it to GitHub first, then run OpenPort on the clone")
    repo = origin_repo(ported)
    name = repo.split("/")[1]
    fork = repo if repo.split("/")[0] == login else f"{login}/{name}"
    base_repo = repo if upstream else fork
    commands = [
        ["git", "checkout", "-B", branch],
        ["git", "add", "-A"],
        ["git", "commit", "-m", title],
    ]
    if fork != repo:
        commands.append(["gh", "repo", "fork", repo, "--clone=false", "--remote=false"])
    commands += [
        ["git", "push", "--force", f"https://github.com/{fork}.git", branch],
        ["gh", "pr", "create", "--repo", base_repo, "--head", branch if base_repo == fork else f"{login}:{branch}",
         "--title", title, "--body-file", str(body_file)],
    ]
    return PrPlan(repo, fork, base_repo, commands)


def github_login() -> str:
    """Your GitHub login. In GitHub Actions, the repo's owner: the branch and the PR stay in the repo itself."""
    if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("GITHUB_REPOSITORY_OWNER"):
        return os.environ["GITHUB_REPOSITORY_OWNER"]
    return _run(["gh", "api", "user", "--jq", ".login"])


def execute(plan: PrPlan, ported: Path) -> str:
    """Run the plan's commands in the ported copy. Returns the PR URL: the branch's open PR if it already has one
    (a weekly upgrade force-pushes its branch, which updates that PR), else the new one."""
    output = ""
    for command in plan.commands:
        try:
            output = _run(command, cwd=ported)
        except subprocess.CalledProcessError as error:
            existing = re.search(r"https://github\.com/\S+/pull/\d+", error.stderr or "")
            if command[:3] == ["gh", "pr", "create"] and "already exists" in (error.stderr or "") and existing:
                return existing[0]
            raise
    return output.splitlines()[-1] if output else ""
