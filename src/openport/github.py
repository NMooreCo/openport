"""Open the port as a PR: commit on a branch, push to your fork, and open the PR there.

The PR is opened inside your fork (fork branch -> fork default branch), so the upstream
maintainers aren't notified. Pass `upstream=True` to propose it to the original repo instead.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH = "openport/nemotron"


@dataclass
class PrPlan:
    repo: str  # owner/name of the original
    fork: str  # owner/name the branch is pushed to
    base_repo: str  # where the PR is opened
    commands: list[list[str]]


def _run(command: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def origin_repo(ported: Path) -> str:
    url = _run(["git", "remote", "get-url", "origin"], cwd=ported)
    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    if match is None:
        raise ValueError(f"origin isn't a GitHub repo: {url}")
    return f"{match[1]}/{match[2]}"


def plan_pr(ported: Path, title: str, body_file: Path, login: str, upstream: bool = False) -> PrPlan:
    if not (ported / ".git").exists():
        raise ValueError(f"{ported} isn't a git repository; push it to GitHub first, then port the clone")
    repo = origin_repo(ported)
    name = repo.split("/")[1]
    fork = repo if repo.split("/")[0] == login else f"{login}/{name}"
    base_repo = repo if upstream else fork
    commands = [
        ["git", "checkout", "-B", BRANCH],
        ["git", "add", "-A"],
        ["git", "commit", "-m", title],
    ]
    if fork != repo:
        commands.append(["gh", "repo", "fork", repo, "--clone=false", "--remote=false"])
    commands += [
        ["git", "push", "--force", f"https://github.com/{fork}.git", BRANCH],
        ["gh", "pr", "create", "--repo", base_repo, "--head", BRANCH if base_repo == fork else f"{login}:{BRANCH}",
         "--title", title, "--body-file", str(body_file)],
    ]
    return PrPlan(repo, fork, base_repo, commands)


def github_login() -> str:
    return _run(["gh", "api", "user", "--jq", ".login"])


def execute(plan: PrPlan, ported: Path) -> str:
    """Run the plan's commands in the ported copy. Returns the PR URL."""
    output = ""
    for command in plan.commands:
        output = _run(command, cwd=ported)
    return output.splitlines()[-1] if output else ""
