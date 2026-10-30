"""Open a pull request in a GitHub organization set aside for it: how the hosted dashboard opens the PRs it writes.

A repo from GitHub is forked into the organization and the PR is opened inside the fork, so the original's
maintainers aren't notified. A demo app, which has no repo on GitHub, gets one there holding its original code.
Either way the PR's branch is the original commit plus exactly the files the run changed, so its diff is the run's.

It takes a token that can create repos in the organization and push to them (administration, contents, pull
requests and workflows): OPENPORT_GITHUB_TOKEN on a server. Nothing here prints it or puts it in a command line.
"""

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

API = "https://api.github.com"
MAX_BODY = 60_000  # GitHub takes 65,536 characters
GIT_TIMEOUT = 300
FORK_WAIT = 90  # seconds a new fork may take to be ready
FALLBACK_AUTHOR = ("OpenPort", "openport@example.com")
# What else the PR body would set off on GitHub: an @mention notifies that person, and a link to an issue or a PR
# elsewhere puts a "mentioned this" on it, as does owner/repo#123. Links go through redirect.github.com instead, as
# Dependabot's do, whichever way they're written (http, www.); a reference to an issue gets a zero-width space. In the
# PR's own repo, #123 and GH-123 link to its issue 123: a changelog quoted from upstream would mention that one.
MENTION = re.compile(r"(?<![\w/.`@-])@(?=[A-Za-z0-9])")
CROSS_REFERENCE = re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/(?=[\w.-]+/[\w.-]+/(?:issues|pull|discussions|commit)/)",
                             re.IGNORECASE)
SHORT_REFERENCE = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(?=\d)")
BARE_REFERENCE = re.compile(r"(?<![\w/&#])#(?=\d)")
GH_REFERENCE = re.compile(r"(?<![\w-])(gh)-(?=\d)", re.IGNORECASE)


class GitHubError(RuntimeError):
    pass


@dataclass
class Opened:
    url: str
    repo: str  # owner/name the PR is in
    new: bool  # False: the branch already had an open PR, which now has this change


Git = Callable[[list[str], Path], None]


def quiet(body: str) -> str:
    """The PR body, minus what would ping people or other repos: @mentions, and links and references to issues and PRs."""
    body = CROSS_REFERENCE.sub("https://redirect.github.com/", MENTION.sub("@\u200b", body))
    body = SHORT_REFERENCE.sub("\\1#\u200b", body)
    return GH_REFERENCE.sub("\\1\u200b-", BARE_REFERENCE.sub("#\u200b", body))


class _Api:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(base_url=API, transport=transport, timeout=30, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kwargs) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        if response.status_code not in ok:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GitHubError(f"GitHub answered {response.status_code} to {method} {path}: {message}")
        return response


def _run_git(token: str) -> Git:
    """git, authenticated by a header from the environment (never the URL or the command line), with no prompts."""
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": header}

    def git(args: list[str], cwd: Path) -> None:
        try:
            # A link in the fetched code is checked out as a file, so a copy can't be led through it out of the repo.
            subprocess.run(["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false", "-c", "core.symlinks=false", *args],
                           cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        except subprocess.CalledProcessError as error:
            raise GitHubError(f"git {args[0]} failed: {(error.stderr or '').strip()[-400:].replace(token, '***')}") from None
        except subprocess.TimeoutExpired:
            raise GitHubError(f"git {args[0]} took over {GIT_TIMEOUT} seconds") from None

    return git


def files_in(folder: Path) -> list[str]:
    """A folder's files, relative to it, without what a port's copy leaves out (dependencies, caches, builds) or links."""
    from .port import COPY_IGNORE

    found = []
    for root, dirs, names in os.walk(folder):
        ignored = COPY_IGNORE(root, dirs + names)
        dirs[:] = sorted(d for d in dirs if d not in ignored and d != ".git" and not (Path(root) / d).is_symlink())
        found += [(Path(root) / n).relative_to(folder).as_posix() for n in names if n not in ignored and not (Path(root) / n).is_symlink()]
    return sorted(found)


def _copy(source: Path, destination: Path, files: list[str]) -> None:
    """Copy `files` (paths relative to `source`) into `destination`, refusing links and anything outside either."""
    source, destination = source.resolve(), destination.resolve()
    for name in files:
        if any(part.lower() == ".git" for part in re.split(r"[\\/]", name)):  # its hooks and config would run on a commit
            raise GitHubError(f"{name} is in .git, which a PR never changes")
        src, dst = source / name, destination / name
        if src.is_symlink() or not src.is_file() or not src.resolve().is_relative_to(source):
            raise GitHubError(f"{name} isn't a file in {source.name}")
        if not dst.resolve().is_relative_to(destination) or dst.is_symlink():
            raise GitHubError(f"{name} would land outside the repo")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _author(api: _Api) -> tuple[str, str]:
    """OpenPort, as the token's owner's GitHub no-reply address: the commits show whose deployment made them."""
    try:
        user = api.call("GET", "/user").json()
        return "OpenPort", f"{user['id']}+{user['login']}@users.noreply.github.com"
    except (GitHubError, httpx.HTTPError, KeyError, ValueError):
        return FALLBACK_AUTHOR


def _fork(api: _Api, org: str, upstream: str) -> tuple[str, str]:
    """The organization's fork of `upstream` (made if there isn't one yet), brought up to date: (full name, default branch)."""
    owner, name = upstream.split("/")
    # GitHub refuses a second fork under a name the organization already uses, so an earlier fork is looked up first
    existing = api.call("GET", f"/repos/{org}/{owner}-{name}", ok=(200, 404))
    if existing.status_code == 200:
        fork = existing.json()
        if (fork.get("parent") or {}).get("full_name", "").lower() != upstream.lower():
            raise GitHubError(f"{org}/{owner}-{name} exists and isn't a fork of {upstream}")
    else:
        fork = api.call("POST", f"/repos/{upstream}/forks", json={"organization": org, "name": f"{owner}-{name}", "default_branch_only": True}).json()
    repo, branch = fork["full_name"], fork["default_branch"]
    deadline = time.monotonic() + FORK_WAIT
    while api.call("GET", f"/repos/{repo}/branches/{branch}", ok=(200, 404, 409)).status_code != 200:  # forks are made in the background
        if time.monotonic() > deadline:
            raise GitHubError(f"GitHub hadn't finished forking {upstream} after {FORK_WAIT} seconds")
        time.sleep(2)
    api.call("POST", f"/repos/{repo}/merge-upstream", ok=(200, 409, 422), json={"branch": branch})  # best effort
    return repo, branch


def _demo_repo(api: _Api, org: str, original: Path, git: Git, author: tuple[str, str]) -> tuple[str, str]:
    """The organization's repo for a demo app, made the first time with the app's original code on main."""
    name = original.resolve().name
    created = api.call("POST", f"/orgs/{org}/repos", ok=(201, 422), json={
        "name": name, "description": f"{name}, an OpenPort demo app: main is the app before OpenPort, the pull requests are its runs",
        "has_wiki": False, "has_projects": False,
    })
    info = created.json() if created.status_code == 201 else api.call("GET", f"/repos/{org}/{name}").json()
    repo, branch = info["full_name"], info.get("default_branch") or "main"
    if not api.call("GET", f"/repos/{repo}/branches", params={"per_page": 1}).json():  # empty: push the original
        with tempfile.TemporaryDirectory(prefix="openport-pr-") as work:
            folder = Path(work)
            _copy(original, folder, files_in(original))
            git(["init", "-q", "-b", branch], folder)
            git(["add", "-A"], folder)
            git(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "commit", "-q", "-m", f"{name} before OpenPort"], folder)
            git(["push", "-q", f"https://github.com/{repo}.git", f"HEAD:refs/heads/{branch}"], folder)
    return repo, branch


def open_pr(
    token: str,
    org: str,
    changed: Path,
    files: list[str],
    title: str,
    body: str,
    branch: str,
    *,
    upstream: str | None = None,
    original: Path | None = None,
    base_commit: str | None = None,
    transport: httpx.BaseTransport | None = None,
    git: Git | None = None,
) -> Opened:
    """Open (or update) a PR in `org` whose change is `files`, copied from the folder `changed`.

    With `upstream` (owner/name on GitHub), it goes in the organization's fork of it, on top of `base_commit`: the
    commit the run started from. Without, `original` is a demo app's folder, whose code its repo starts with."""
    if not files:
        raise GitHubError("The run changed no files, so there's no pull request to open")
    if upstream is None and original is None:
        raise GitHubError("A pull request needs the GitHub repo it's for, or the demo app's original folder")
    api = _Api(token, transport)
    git = git or _run_git(token)
    author = _author(api)
    if upstream:
        repo, base = _fork(api, org, upstream)
    else:
        repo, base = _demo_repo(api, org, original, git, author)
    # The organization's repos don't run Actions: the PR's workflow would fail there without the repo's secrets.
    api.call("PUT", f"/repos/{repo}/actions/permissions", ok=(204, 403, 409), json={"enabled": False})

    url = f"https://github.com/{repo}.git"
    with tempfile.TemporaryDirectory(prefix="openport-pr-") as work:
        folder = Path(work)
        git(["init", "-q"], folder)
        try:
            git(["fetch", "-q", "--depth", "1", url, base_commit or base], folder)
        except GitHubError:
            if not base_commit:
                raise
            git(["fetch", "-q", "--depth", "1", url, base], folder)  # the commit is gone from the original: its branch then
        git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], folder)
        _copy(changed, folder, files)
        git(["add", "-A"], folder)
        git(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "commit", "-q", "-m", title], folder)
        git(["push", "-q", "--force", url, f"HEAD:refs/heads/{branch}"], folder)

    owner = repo.split("/")[0]
    text = quiet(body)
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n\n…(cut to fit GitHub's limit)"
    existing = api.call("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}).json()
    if existing:
        number = existing[0]["number"]
        pr = api.call("PATCH", f"/repos/{repo}/pulls/{number}", json={"title": title, "body": text}).json()
        return Opened(pr["html_url"], repo, False)
    pr = api.call("POST", f"/repos/{repo}/pulls", json={
        "title": title, "head": branch, "base": base, "body": text, "maintainer_can_modify": False,
    }).json()
    return Opened(pr["html_url"], repo, True)
