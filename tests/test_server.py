"""The dashboard pipeline end to end, offline: a tiny app, a fake Token Factory, the real tuning engineer."""

import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from openport import config, tokenfactory
from openport.server import app, pipeline, runs

from .fakes import FakeTokenFactory, completion

MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}
LABELS = {"refund please": "billing", "app crashed": "technical", "love it": "praise", "where is my box": "shipping"}

MINI_APP = {
    "miniapp/__init__.py": "",
    "miniapp/classify.py": '''import json

from openai import OpenAI

client = OpenAI()

SYSTEM = """Label the message as billing, technical, praise or shipping.
Respond with JSON: {"label": "<label>"}"""


def classify(text):
    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
    )
    return json.loads(response.choices[0].message.content)
''',
    "evals/openport.evals.yaml": '''target: miniapp
source_root: ..
call_sites:
  - id: classify
    title: Message classifier
    file: miniapp/classify.py
    module: miniapp.classify
    function: classify
    client_attr: client
    sdk: openai
    original_model: gpt-5-mini
    dataset: classify.jsonl
    grader: {type: fields, fields: [label]}
''',
    "evals/classify.jsonl": "".join(
        json.dumps({"id": f"c{i}", "input": {"text": text}, "expected": {"label": label}}) + "\n"
        for i, (text, label) in enumerate(LABELS.items())
    ),
}


NOSPEC_APP = {"miniapp/__init__.py": "", "miniapp/classify.py": MINI_APP["miniapp/classify.py"]}

TUNED = {"reasoning": False, "system_addendum": "Output the JSON object and nothing else.", "max_tokens": None, "temperature": None, "rationale": "chatter around JSON"}


def _reply(request):
    """One fake Token Factory reply, covering tuning, capture input generation and capture labelling."""
    system = request["messages"][0]["content"]
    user = request["messages"][-1]["content"]
    if system.startswith("You write test inputs"):  # capture: Super invents inputs
        return completion(content=json.dumps([{"text": text} for text in LABELS]))
    if system.startswith("You produce the reference output"):  # capture: Ultra labels one input
        text = next((t for t in LABELS if t in user), next(iter(LABELS)))
        return completion(content=json.dumps({"label": LABELS[text]}))
    if system.startswith("You tune LLM calls"):  # the engineer (Nemotron Ultra)
        return completion(content=json.dumps(TUNED))
    answer = json.dumps({"label": LABELS[user]})
    if "nothing else" in system:
        return completion(content=answer)
    return completion(content=f"Here is the label: {answer}")  # the plain swap wraps its JSON in chatter


def fake_token_factory():
    return FakeTokenFactory(_reply)


@pytest.fixture
def server(tmp_path, monkeypatch):
    for path, text in MINI_APP.items():
        (tmp_path / "examples/mini" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "examples/mini" / path).write_text(text)
    fake = fake_token_factory()
    monkeypatch.setattr(pipeline, "EXAMPLES", tmp_path / "examples")
    monkeypatch.setattr(pipeline, "PRESETS", [{"id": "mini", "label": "Mini", "detail": "test app", "path": "mini"}])
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(tokenfactory, "client", lambda: fake)
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    runs.clear()
    yield TestClient(app)
    runs.clear()
    sys.modules.pop("miniapp.classify", None)
    sys.modules.pop("miniapp", None)


@pytest.fixture
def server_nospec(tmp_path, monkeypatch):
    for path, text in NOSPEC_APP.items():
        (tmp_path / "examples/mini2" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "examples/mini2" / path).write_text(text)
    fake = fake_token_factory()
    monkeypatch.setattr(pipeline, "EXAMPLES", tmp_path / "examples")
    monkeypatch.setattr(pipeline, "PRESETS", [{"id": "mini2", "label": "Mini2", "detail": "no-spec app", "path": "mini2"}])
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(tokenfactory, "client", lambda: fake)
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    runs.clear()
    yield TestClient(app)
    runs.clear()
    sys.modules.pop("miniapp.classify", None)
    sys.modules.pop("miniapp", None)


PINNED = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def server_pack(tmp_path, monkeypatch):
    """Public repos: owner/mini ships no eval spec and has an OpenPort eval pack pinned to a commit; owner/unpinned
    has a pack that isn't pinned; owner/withspec ships its own spec. Yields the client and the repos fetched."""
    clones = {"owner/mini": NOSPEC_APP, "owner/unpinned": NOSPEC_APP, "owner/withspec": MINI_APP}
    for repo, files in clones.items():
        for path, text in files.items():
            (tmp_path / "clones" / repo / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / "clones" / repo / path).write_text(text)
    for repo, source in (("mini", f"owner/mini@{PINNED}"), ("unpinned", "owner/unpinned")):
        pack = tmp_path / "examples" / "evalpacks" / repo
        pack.mkdir(parents=True)
        (pack / "openport.evals.yaml").write_text(MINI_APP["evals/openport.evals.yaml"].replace("source_root: ..", f"source_root: repo:{source}"))
        (pack / "classify.jsonl").write_text(MINI_APP["evals/classify.jsonl"])
    fetched = []

    def fetch(target):
        fetched.append(target)
        return tmp_path / "clones" / target.partition("@")[0], False

    fake = fake_token_factory()
    monkeypatch.setattr(pipeline, "EXAMPLES", tmp_path / "examples")
    monkeypatch.setattr(pipeline, "PRESETS", [{"id": "mini3", "label": "owner/mini", "detail": "remote app", "repo": "owner/mini"},
                                              {"id": "loose", "label": "owner/unpinned", "detail": "remote app", "repo": "owner/unpinned"}])
    monkeypatch.setattr(pipeline, "fetch_repo", fetch)
    monkeypatch.setattr("openport.workspace.fetch_repo", fetch)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(tokenfactory, "client", lambda: fake)
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    runs.clear()
    yield TestClient(app), fetched
    runs.clear()
    sys.modules.pop("miniapp.classify", None)
    sys.modules.pop("miniapp", None)


AUTHKIT = __import__("pathlib").Path(__file__).parents[1] / "examples" / "authkit"


@pytest.fixture
def upgrade_server(tmp_path, monkeypatch):
    """The upgrade pipeline on the AuthKit example, with OSV, the test container and Super scripted."""
    from datetime import datetime, timezone

    from openport.server import upgrades
    from openport.upgrade import Upgrade
    from openport.upgrade.apply import Edit
    from openport.upgrade.lockfiles import Dependency
    from openport.upgrade.osv import Advisory
    from openport.upgrade.select import Target
    from openport.upgrade.testrun import TestRun

    plan = [Upgrade(Dependency("PyJWT", "1.7.1", "PyPI", "requirements.txt"),
                    [Advisory("GHSA-ffqj-6fqr-9h24", "Key confusion", ["CVE-2022-29217"])],
                    Target("2.14.0", datetime(2026, 9, 11, tzinfo=timezone.utc), 43, ["CVE-2022-29217"], []), "upgrade")]
    results = iter([TestRun(True, 0, "5 passed", "", 1.0), TestRun(False, 1, "4 failed, 1 passed", "AttributeError", 1.0),
                    TestRun(True, 0, "5 passed", "", 1.0)])
    fix = lambda *args: ([Edit("authkit/tokens.py", 'return token.decode("utf-8")', "return token")], "PyJWT 2 returns str", [])
    monkeypatch.setattr(upgrades, "plan_upgrades", lambda deps: plan)
    monkeypatch.setattr("openport.upgrade.testrun.run_tests", lambda folder, *, image, command: next(results))  # the real signature
    monkeypatch.setattr("openport.upgrade.fix.make_fixer", lambda *, meter=None: fix)
    monkeypatch.setattr(upgrades.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(upgrades, "PRESETS", [{"id": "authkit", "label": "AuthKit", "detail": "demo", "path": "authkit"}])
    monkeypatch.setattr(pipeline, "EXAMPLES", AUTHKIT.parent)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    runs.clear()
    yield TestClient(app)
    runs.clear()


def wait_for(client, run_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("done", "error"):
            return run
        time.sleep(0.05)
    raise TimeoutError(run)


def test_only_presets_and_github_repos(server):
    assert server.post("/api/runs", json={"target": "C:/Windows"}).status_code == 400
    assert server.post("/api/runs", json={"target": "../../etc"}).status_code == 400
    assert server.post("/api/runs", json={"target": "../etc"}).status_code == 400
    assert [p["id"] for p in server.get("/api/presets").json()] == ["mini"]


def test_pipeline_end_to_end(server):
    started = server.post("/api/runs", json={"target": "mini"}).json()
    run = wait_for(server, started["id"])
    assert run["status"] == "done", run["events"][-1]
    events = run["events"]

    assert [e["stage"] for e in events if e["type"] == "stage"] == ["fetch", "scan", "tune", "port", "pr"]
    scan = next(e for e in events if e["type"] == "scan")
    assert [s["id"] for s in scan["sites"]] == ["classify"]

    attempts = [e for e in events if e["type"] == "attempt"]
    assert [(a["tier"], a["round"], a["meets"]) for a in attempts] == [("nano", 0, False), ("nano", 1, True)]
    assert attempts[1]["addendum"] == "Output the JSON object and nothing else."

    ported = next(e for e in events if e["type"] == "ported")
    assert ported["sites"][0]["status"] == "ported" and ported["sites"][0]["now"] == "nvidia/nano-test"
    assert "Output the JSON object and nothing else." in ported["diff"]  # the tuned prompt is in the code
    assert ".github/workflows/openport.yml" in ported["added"]

    body = next(e for e in events if e["type"] == "pr")["body"]
    assert "| `classify` " in body and "| 0% | 100% (4 cases) |" in body

    # The live stream replays the same events, and asking again soon reuses the finished run.
    stream = server.get(f"/api/runs/{started['id']}/events").text
    assert stream.count("event: attempt") == 2 and "event: done" in stream
    again = server.post("/api/runs", json={"target": "mini"}).json()
    assert again["reused"] and again["id"] == started["id"]


def _recorded(target="mini", kind="port") -> "pipeline.Run":
    events = [{"seq": i, "type": kind_, "time": 100.0 + 10 * i, **data} for i, (kind_, data) in
              enumerate([("stage", {"stage": "fetch"}), ("cost", {"evals_usd": 0.4, "helper_usd": 0.3}), ("done", {})])]
    return pipeline.Run(id="rec" + "0" * 9, target=target, label="Mini", status="done", created=100.0, events=events, kind=kind, recorded=True)


def test_a_recorded_run_is_shown_until_someone_asks_for_a_live_one(server, monkeypatch):
    import openport.server as srv

    recorded = _recorded()
    runs[recorded.id] = recorded
    shown = server.post("/api/runs", json={"target": "mini"}).json()
    assert (shown["id"], shown["reused"], shown["recorded"]) == (recorded.id, True, True)

    monkeypatch.setattr(srv, "REPLAY_SPEED", 1000)  # the replay keeps the recorded timing, sped up
    stream = server.get(f"/api/runs/{recorded.id}/events").text
    assert stream.count("event: ") == 3 and "event: done" in stream

    live = server.post("/api/runs", json={"target": "mini", "live": True}).json()
    assert live["id"] != recorded.id and not live["reused"] and not live["recorded"]
    assert wait_for(server, live["id"])["status"] == "done"


def test_live_runs_stop_at_the_daily_budget(server, monkeypatch):
    monkeypatch.setattr(pipeline, "DAILY_BUDGET_USD", 0.5)
    spent = pipeline.Run(id="spent0000001", target="other", label="x", status="done",
                         events=[{"seq": 0, "type": "cost", "time": 0, "evals_usd": 0.4, "helper_usd": 0.2}])
    runs[spent.id] = spent
    assert server.get("/api/info").json()["daily_budget_usd"] == 0.5
    response = server.post("/api/runs", json={"target": "mini"})
    assert response.status_code == 429 and "budget for today ($0.5) is used up" in response.json()["detail"]
    runs[_recorded().id] = _recorded()  # a recorded run costs nothing, so it's still shown
    assert server.post("/api/runs", json={"target": "mini"}).json()["recorded"]


def test_finished_runs_load_from_disk(tmp_path):
    def save(name, **fields):
        (tmp_path / name).write_text(json.dumps({"id": name[:-5], "kind": "upgrade", "target": "authkit", "label": "AuthKit",
                                                 "status": "done", "created": 1.0, "events": [], **fields}))

    save("a.json")
    save("b.json", status="running")  # never finished: not a result to show
    (tmp_path / "c.json").write_text("{not json")
    [run] = pipeline.load_runs(tmp_path, recorded=True)
    assert (run.id, run.kind, run.recorded, run.cost_usd) == ("a", "upgrade", True, 0)


def test_upgrade_pipeline_proves_the_upgrade(upgrade_server):
    assert [p["id"] for p in upgrade_server.get("/api/upgrade-presets").json()] == ["authkit"]
    started = upgrade_server.post("/api/upgrades", json={"target": "authkit"}).json()
    assert started["kind"] == "upgrade"
    run = wait_for(upgrade_server, started["id"])
    assert run["status"] == "done", run["events"][-1]
    events = run["events"]
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["fetch", "find", "test", "fix", "pr"]
    package = next(e for e in events if e["type"] == "advisories")["packages"][0]
    assert (package["name"], package["target"], package["fixed"]) == ("PyJWT", "2.14.0", ["CVE-2022-29217"])
    assert [(e["step"], e["ok"]) for e in events if e["type"] == "tests"] == [("before", True), ("after_bump", False)]
    fix = next(e for e in events if e["type"] == "fix")
    assert fix["files"] == ["authkit/tokens.py"] and fix["tests"]["ok"]
    pr = next(e for e in events if e["type"] == "upgrade_pr")
    assert pr["proven"] and "+PyJWT==2.14.0" in pr["diff"] and "| Before the upgrade | ✅ 5 passed |" in pr["body"]
    assert next(e for e in events if e["type"] == "bumped")["packages"] == ["PyJWT"]
    assert next(e for e in events if e["type"] == "cost") == {**next(e for e in events if e["type"] == "cost"), "evals_usd": 0.0, "helper_usd": 0.0}


def test_a_monorepo_project_is_upgraded_from_its_folders_github_url(upgrade_server, tmp_path, monkeypatch):
    import shutil

    from openport.server import upgrades
    from openport.workspace import split_project

    assert split_project("https://github.com/acme/mono/tree/main/apps/auth") == ("acme/mono", "apps/auth")
    assert split_project("acme/mono") == ("acme/mono", "")
    mono = tmp_path / "acme__mono"
    shutil.copytree(AUTHKIT, mono / "apps" / "auth")  # AuthKit as one project of a monorepo
    monkeypatch.setattr(upgrades, "fetch_repo", lambda repo: (mono, False) if repo == "acme/mono" else (_ for _ in ()).throw(ValueError(repo)))
    assert upgrade_server.post("/api/upgrades", json={"target": "https://github.com/acme/mono/tree/main/../../etc"}).status_code == 400

    started = upgrade_server.post("/api/upgrades", json={"target": "https://github.com/acme/mono/tree/main/apps/auth"}).json()
    run = wait_for(upgrade_server, started["id"])
    assert run["status"] == "done" and run["target"] == "acme/mono/apps/auth", run["events"][-1]
    fix = next(e for e in run["events"] if e["type"] == "fix")
    assert fix["files"] == ["authkit/tokens.py"] and fix["tests"]["ok"]  # paths start at the project, as its tests see them
    diff = next(e for e in run["events"] if e["type"] == "upgrade_pr")["diff"]
    assert "+++ b/apps/auth/requirements.txt" in diff and "+++ b/apps/auth/authkit/tokens.py" in diff


def test_upgrade_without_docker_is_a_dry_run(upgrade_server, monkeypatch):
    from openport.server import upgrades

    monkeypatch.setattr(upgrades.shutil, "which", lambda name: None)
    run = wait_for(upgrade_server, upgrade_server.post("/api/upgrades", json={"target": "authkit"}).json()["id"])
    assert run["status"] == "done"
    assert [e["stage"] for e in run["events"] if e["type"] == "stage"] == ["fetch", "find"]
    assert any("Dry run" in e.get("message", "") for e in run["events"])


def test_pipeline_uses_an_eval_pack_for_a_repo_without_a_spec(server_pack):
    client, fetched = server_pack
    started = client.post("/api/runs", json={"target": "mini3"}).json()
    run = wait_for(client, started["id"])
    assert run["status"] == "done", run["events"][-1]
    events = run["events"]
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["fetch", "scan", "tune", "port", "pr"]  # no capture
    notes = [e["message"] for e in events if e["type"] == "note"]
    assert any("eval pack" in note and "4 hand-labelled cases" in note for note in notes)
    assert fetched[0] == f"owner/mini@{PINNED}" and any("at 0123456" in note for note in notes)  # the port is the commit tested
    assert next(e for e in events if e["type"] == "ported")["sites"][0]["status"] == "ported"


def test_a_public_server_runs_only_vetted_repo_code(server_pack, monkeypatch):
    client, _ = server_pack
    assert [p["tested"] for p in client.get("/api/presets").json()] == [True, True]  # locally, any repo's code may run
    monkeypatch.setattr(pipeline, "VETTED_ONLY", True)
    assert client.get("/api/info").json()["vetted_only"] is True
    assert [p["tested"] for p in client.get("/api/presets").json()] == [True, False]  # the launcher says so up front

    def stages(target):
        run = wait_for(client, client.post("/api/runs", json={"target": target}).json()["id"])
        assert run["status"] == "done", run["events"][-1]
        return [e["stage"] for e in run["events"] if e["type"] == "stage"], [e["message"] for e in run["events"] if e["type"] == "note"]

    assert stages("owner/mini")[0] == ["fetch", "scan", "tune", "port", "pr"]  # an eval pack pinned to a reviewed commit
    for repo in ("owner/withspec", "owner/unpinned"):  # the repo's own spec, or a pack that follows the branch
        ran, notes = stages(repo)
        assert ran == ["fetch", "scan", "port", "pr"] and any("this public demo" in note for note in notes), repo


def test_pipeline_captures_when_no_spec(server_nospec):
    started = server_nospec.post("/api/runs", json={"target": "mini2"}).json()
    run = wait_for(server_nospec, started["id"])
    assert run["status"] == "done", run["events"][-1]
    events = run["events"]

    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages == ["fetch", "scan", "capture", "tune", "port", "pr"]

    captured = next(e for e in events if e["type"] == "captured")
    assert captured["sites"][0] == {"site": "classify", "cases": 4, "grader": "fields"}
    assert "not been reviewed" in captured["note"]

    assert any(e["type"] == "attempt" for e in events)  # the generated spec is tuned
    ported = next(e for e in events if e["type"] == "ported")
    assert ported["sites"][0]["status"] == "ported"
    assert next(e for e in events if e["type"] == "pr")["body"]
