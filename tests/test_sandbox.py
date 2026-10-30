"""Opening a run's pull request in the sandbox organization, against a fake GitHub API and a git that only records."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import openport.server as server
from openport import config, sandbox
from openport.port import _diff
from openport.server import app, runs
from openport.server.pipeline import Run


class FakeGitHub:
    """GitHub's REST API, just the calls sandbox.open_pr makes, with what it was asked kept for the asserts."""

    def __init__(self, existing_pr: bool = False, repo_exists: bool = False, fork_of: str | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.existing_pr = existing_pr
        self.repo_exists = repo_exists
        self.fork_of = fork_of  # the organization already holds sandbox/acme-app, a fork of this repo
        self.fork_polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer t0ken"
        if path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "owner"})
        if path == "/repos/sandbox/acme-app" and request.method == "GET":
            if self.fork_of is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"full_name": "sandbox/acme-app", "default_branch": "main", "fork": True,
                                             "parent": {"full_name": self.fork_of}})
        if path.endswith("/forks"):
            return httpx.Response(202, json={"full_name": "sandbox/acme-app", "default_branch": "main"})
        if path == "/repos/sandbox/acme-app/branches/main":
            self.fork_polls += 1
            return httpx.Response(404 if self.fork_polls == 1 else 200, json={})  # a fork takes a moment
        if path == "/orgs/sandbox/repos":
            return httpx.Response(422 if self.repo_exists else 201, json={"full_name": "sandbox/mini", "default_branch": "main"})
        if path == "/repos/sandbox/mini" and request.method == "GET":
            return httpx.Response(200, json={"full_name": "sandbox/mini", "default_branch": "main"})
        if path == "/repos/sandbox/mini/branches":
            return httpx.Response(200, json=[{"name": "main"}] if self.repo_exists else [])
        if path.endswith("/merge-upstream"):
            return httpx.Response(200, json={})
        if path.endswith("/actions/permissions"):
            return httpx.Response(204)
        if path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(200, json=[{"number": 3}] if self.existing_pr else [])
        if path.endswith("/pulls") and request.method == "POST":
            return httpx.Response(201, json={"html_url": f"https://github.com{path.removeprefix('/repos').removesuffix('/pulls')}/pull/1"})
        if "/pulls/" in path and request.method == "PATCH":
            return httpx.Response(200, json={"html_url": f"https://github.com{path.removeprefix('/repos').replace('/pulls/', '/pull/')}"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})


class RecordingGit:
    """git that records each command, and what the work tree held when each commit was made."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.committed: list[dict[str, str]] = []

    def __call__(self, args: list[str], cwd: Path) -> None:
        self.commands.append(args)
        if "commit" in args:
            self.committed.append({p.relative_to(cwd).as_posix(): p.read_text() for p in cwd.rglob("*") if p.is_file()})


def ported(tmp_path: Path) -> Path:
    folder = tmp_path / "ported"
    (folder / "app").mkdir(parents=True)
    (folder / "app/llm.py").write_text("client = token_factory_client()\n")
    (folder / "app/openport_llm.py").write_text("# runtime\n")
    (folder / "untouched.py").write_text("not part of the change\n")
    return folder


def test_a_repo_from_github_is_forked_and_the_pr_opened_inside_the_fork(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.time, "sleep", lambda _: None)
    api, git = FakeGitHub(), RecordingGit()
    opened = sandbox.open_pr("t0ken", "sandbox", ported(tmp_path), ["app/llm.py", "app/openport_llm.py"], "Move to Nemotron", "Body",
                             "openport/nemotron-1", upstream="acme/app", base_commit="a" * 40, transport=httpx.MockTransport(api), git=git)
    assert opened == sandbox.Opened("https://github.com/sandbox/acme-app/pull/1", "sandbox/acme-app", True)
    fork = next(body for method, path, body in api.calls if path == "/repos/acme/app/forks")
    assert fork == {"organization": "sandbox", "name": "acme-app", "default_branch_only": True}
    assert api.fork_polls == 2  # waited for the fork
    assert ("PUT", "/repos/sandbox/acme-app/actions/permissions", {"enabled": False}) in api.calls
    # The branch is the commit the run started from, plus the changed files and nothing else.
    assert ["fetch", "-q", "--depth", "1", "https://github.com/sandbox/acme-app.git", "a" * 40] in git.commands
    assert git.committed == [{"app/llm.py": "client = token_factory_client()\n", "app/openport_llm.py": "# runtime\n"}]
    assert ["push", "-q", "--force", "https://github.com/sandbox/acme-app.git", "HEAD:refs/heads/openport/nemotron-1"] in git.commands
    pr = next(body for method, path, body in api.calls if method == "POST" and path.endswith("/pulls"))
    assert pr["head"] == "openport/nemotron-1" and pr["base"] == "main" and pr["body"] == "Body"
    # The commits carry the token owner's no-reply address.
    commit = next(c for c in git.commands if "commit" in c)
    assert "user.email=7+owner@users.noreply.github.com" in commit


def test_a_demo_app_gets_a_repo_holding_its_original_code(tmp_path):
    original = tmp_path / "mini"
    (original / "app/__pycache__").mkdir(parents=True)
    (original / "app/llm.py").write_text("client = OpenAI()\n")
    (original / "app/__pycache__/llm.pyc").write_text("cache")
    api, git = FakeGitHub(), RecordingGit()
    opened = sandbox.open_pr("t0ken", "sandbox", ported(tmp_path), ["app/llm.py"], "Move to Nemotron", "Body", "openport/nemotron",
                             original=original, transport=httpx.MockTransport(api), git=git)
    assert opened.url == "https://github.com/sandbox/mini/pull/1"
    assert git.committed[0] == {"app/llm.py": "client = OpenAI()\n"}  # main: the app as it was, without its caches
    assert ["push", "-q", "https://github.com/sandbox/mini.git", "HEAD:refs/heads/main"] in git.commands
    assert git.committed[1] == {"app/llm.py": "client = token_factory_client()\n"}


def test_an_open_pr_for_the_branch_is_updated_not_duplicated(tmp_path):
    api, git = FakeGitHub(existing_pr=True, repo_exists=True), RecordingGit()
    opened = sandbox.open_pr("t0ken", "sandbox", ported(tmp_path), ["app/llm.py"], "Move to Nemotron", "Body", "openport/nemotron",
                             original=tmp_path / "mini", transport=httpx.MockTransport(api), git=git)
    assert opened == sandbox.Opened("https://github.com/sandbox/mini/pull/3", "sandbox/mini", False)
    assert len(git.committed) == 1  # the repo already had its original code
    assert not any(method == "POST" and path.endswith("/pulls") for method, path, _ in api.calls)


def test_nothing_outside_the_ported_folder_is_copied(tmp_path):
    folder = ported(tmp_path)
    (tmp_path / "secret.txt").write_text("keys")
    for name in ("../secret.txt", "app/missing.py"):
        with pytest.raises(sandbox.GitHubError):
            sandbox.open_pr("t0ken", "sandbox", folder, [name], "T", "B", "b", upstream="acme/app",
                            transport=httpx.MockTransport(FakeGitHub()), git=RecordingGit())


def test_the_body_pings_nobody():
    body = "Thanks @someone, see https://github.com/acme/app/issues/4 and https://github.com/acme/app (mail me@example.com)"
    assert sandbox.quiet(body) == ("Thanks @​someone, see https://redirect.github.com/acme/app/issues/4 and "
                                   "https://github.com/acme/app (mail me@example.com)")


def test_the_files_of_a_demo_app_leave_out_dependencies_and_caches(tmp_path):
    for name in ("a.py", "pkg/b.py", "node_modules/x.js", "pkg/__pycache__/b.pyc", ".git/config"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    assert sandbox.files_in(tmp_path) == ["a.py", "pkg/b.py"]


@pytest.fixture
def finished_port(tmp_path, monkeypatch):
    """A finished live port of acme/app, as the pipeline leaves it: the clone, the ported copy, and the run."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    clone, out = tmp_path / "runs/repos/acme__app", tmp_path / "runs/ports/acme__app"
    for folder, text in ((clone, "client = OpenAI()\n"), (out, "client = token_factory_client()\n")):
        (folder / "app").mkdir(parents=True)
        (folder / "app/llm.py").write_text(text)
    run = Run(id="abc123", target="acme/app", label="acme/app", status="done")
    run.emit("ported", sites=[], changed=["app/llm.py"], added=[], diff=_diff(clone, out, ["app/llm.py"]))
    run.emit("pr", body="The PR", rows=[])
    run.emit("done")
    runs.clear()
    runs[run.id] = run
    monkeypatch.setattr(server, "GITHUB_TOKEN", "t0ken")
    monkeypatch.setattr(server, "SANDBOX_ORG", "sandbox")
    yield TestClient(app), run, out
    runs.clear()


def test_the_dashboard_opens_a_finished_ports_pr_once(finished_port, monkeypatch):
    client, run, out = finished_port
    asked = []

    def open_pr(token, org, changed, files, title, body, branch, **kwargs):
        asked.append((token, org, changed, files, branch, kwargs["upstream"], body))
        return sandbox.Opened("https://github.com/sandbox/acme-app/pull/1", "sandbox/acme-app", True)

    monkeypatch.setattr(sandbox, "open_pr", open_pr)
    assert client.get("/api/site").json()["github_org"] == "sandbox"
    first = client.post(f"/api/runs/{run.id}/github")
    assert first.json() == {"url": "https://github.com/sandbox/acme-app/pull/1", "opened": True}
    token, org, changed, files, branch, upstream, body = asked[0]
    assert (token, org, changed, files, branch, upstream) == ("t0ken", "sandbox", out, ["app/llm.py"], "openport/nemotron-abc123", "acme/app")
    assert "maintainers weren't notified" in body and body.endswith("The PR")
    # Asked again, it's the same PR: GitHub isn't asked twice, and the run (and its saved copy) remember it.
    assert client.post(f"/api/runs/{run.id}/github").json() == {"url": "https://github.com/sandbox/acme-app/pull/1", "opened": False}
    assert len(asked) == 1
    assert client.get(f"/api/runs/{run.id}").json()["github"] == "https://github.com/sandbox/acme-app/pull/1"
    saved = json.loads((config.RUNS_DIR / "dashboard" / f"{run.id}.json").read_text())
    assert saved["github"] == "https://github.com/sandbox/acme-app/pull/1"


def test_opening_a_pr_is_for_judges_and_capped_like_live_runs(finished_port):
    client, run, _ = finished_port
    for_judges = client.post(f"/api/runs/{run.id}/github", headers={"x-openport-reuse-only": "judges"})
    assert for_judges.status_code == 403 and "judges" in for_judges.json()["detail"]
    capped = client.post(f"/api/runs/{run.id}/github", headers={"x-openport-reuse-only": "1"})
    assert capped.status_code == 429


def test_a_port_overwritten_by_a_later_run_isnt_opened(finished_port, monkeypatch):
    client, run, out = finished_port
    monkeypatch.setattr(sandbox, "open_pr", lambda *a, **k: pytest.fail("opened a PR for another run's port"))
    (out / "app/llm.py").write_text("client = something_else()\n")
    refused = client.post(f"/api/runs/{run.id}/github")
    assert refused.status_code == 409 and "ported again" in refused.json()["detail"]


def test_without_a_token_the_server_says_so(finished_port, monkeypatch):
    client, run, _ = finished_port
    monkeypatch.setattr(server, "GITHUB_TOKEN", None)
    assert client.get("/api/site").json()["github_org"] is None
    assert client.post(f"/api/runs/{run.id}/github").status_code == 503


def test_a_fork_the_organization_already_has_is_reused(tmp_path, monkeypatch):
    # GitHub answers a second fork request under the same name with 403 "Name already exists", so it mustn't be made
    api = FakeGitHub(fork_of="acme/app")
    folder = ported(tmp_path)
    opened = sandbox.open_pr("t0ken", "sandbox", folder, ["app/llm.py"], "Title", "Body", "openport/nemotron",
                             upstream="acme/app", base_commit="abc123", transport=httpx.MockTransport(api), git=RecordingGit())
    assert opened.repo == "sandbox/acme-app" and opened.new
    assert not any(path.endswith("/forks") for _, path, _ in api.calls)


def test_a_repo_of_the_forks_name_that_isnt_a_fork_of_it_is_refused(tmp_path):
    api = FakeGitHub(fork_of="someone/else")
    with pytest.raises(sandbox.GitHubError, match="isn't a fork of acme/app"):
        sandbox.open_pr("t0ken", "sandbox", ported(tmp_path), ["app/llm.py"], "Title", "Body", "openport/nemotron",
                        upstream="acme/app", base_commit="abc123", transport=httpx.MockTransport(api), git=RecordingGit())
