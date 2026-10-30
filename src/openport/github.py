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
# Makes your fork of a repo, or answers with the one you have, whatever it's called: + ["repos/OWNER/NAME/forks", ...].
FORK = ["gh", "api", "--method", "POST"]


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


def posted_body(body_file: Path) -> Path:
    """The PR body as it's posted, beside the one written: @mentions and references to other repos' issues and PRs
    defused (sandbox.quiet), so opening the PR pings nobody. Written when `body_file` exists."""
    from .sandbox import quiet

    posted = body_file.with_name(f"{body_file.stem}.posted{body_file.suffix}")
    if body_file.is_file():
        posted.write_text(quiet(body_file.read_text(encoding="utf-8")), encoding="utf-8")
    return posted


def plan_pr(ported: Path, title: str, body_file: Path, login: str, upstream: bool = False, branch: str = BRANCH) -> PrPlan:
    """The commands that open the PR. An upgrade's folder (upgrade.apply.make_pr_folder) is the original's last commit
    plus the changed files, so `git add -A` there stages just those, never work in progress from the original."""
    if not (ported / ".git").exists():
        raise ValueError(f"{ported} isn't a git repository; push it to GitHub first, then run OpenPort on the clone")
    repo = origin_repo(ported)
    owner, name = repo.split("/")
    # Your fork as a new one is named. One you have may be named otherwise (renamed, or name-1 beside a repo of yours
    # called name that isn't a fork): execute() pushes to the fork GitHub names, never to a repo that only shares the name.
    fork = repo if owner.lower() == login.lower() else f"{login}/{name}"
    base_repo = repo if upstream else fork
    commands = [
        ["git", "checkout", "-B", branch],
        ["git", "add", "-A"],
        ["git", "commit", "-m", title],
    ]
    if fork != repo:
        commands.append([*FORK, f"repos/{repo}/forks", "--jq", ".full_name"])
    commands += [
        ["git", "push", "--force", f"https://github.com/{fork}.git", branch],
        ["gh", "pr", "create", "--repo", base_repo, "--head", branch if base_repo == fork else f"{login}:{branch}",
         "--title", title, "--body-file", str(posted_body(body_file))],
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
    output, fork = "", plan.fork
    for command in plan.commands:
        if fork != plan.fork:  # your fork has another name than a new one would: push to it, and open the PR there
            names = {plan.fork: fork, f"https://github.com/{plan.fork}.git": f"https://github.com/{fork}.git"}
            command = [names.get(arg, arg) for arg in command]
        try:
            output = _run(command, cwd=ported)
        except subprocess.CalledProcessError as error:
            existing = re.search(r"https://github\.com/\S+/pull/\d+", error.stderr or "")
            if command[:3] == ["gh", "pr", "create"] and "already exists" in (error.stderr or "") and existing:
                return existing[0]
            raise
        if command[:len(FORK)] == FORK:
            fork = _fork_name(output, plan.fork)
    return output.splitlines()[-1] if output else ""


def _fork_name(output: str, planned: str) -> str:
    """The fork GitHub answered with: yours (the planned fork's owner), whatever it's called. Anything else stops the
    run, so nothing is pushed to a repo that isn't your fork."""
    name = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", name) or name.split("/")[0].lower() != planned.split("/")[0].lower():
        raise ValueError(f"GitHub didn't answer with a fork of yours (it said {name!r}), so nothing was pushed")
    return name
