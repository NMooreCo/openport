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


@pytest.fixture
def server_pack(tmp_path, monkeypatch):
    """A public repo with no eval spec, and an OpenPort eval pack for it."""
    clone = tmp_path / "clones" / "owner__mini"
    for path, text in NOSPEC_APP.items():
        (clone / path).parent.mkdir(parents=True, exist_ok=True)
        (clone / path).write_text(text)
    pack = tmp_path / "examples" / "evalpacks" / "mini"
    pack.mkdir(parents=True)
    spec = MINI_APP["evals/openport.evals.yaml"].replace("source_root: ..", "source_root: repo:owner/mini")
    (pack / "openport.evals.yaml").write_text(spec)
    (pack / "classify.jsonl").write_text(MINI_APP["evals/classify.jsonl"])
    fake = fake_token_factory()
    monkeypatch.setattr(pipeline, "EXAMPLES", tmp_path / "examples")
    monkeypatch.setattr(pipeline, "PRESETS", [{"id": "mini3", "label": "owner/mini", "detail": "remote app", "repo": "owner/mini"}])
    monkeypatch.setattr(pipeline, "fetch_repo", lambda target: (clone, False))
    monkeypatch.setattr("openport.workspace.fetch_repo", lambda target: (clone, False))
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


def test_pipeline_uses_an_eval_pack_for_a_repo_without_a_spec(server_pack):
    started = server_pack.post("/api/runs", json={"target": "mini3"}).json()
    run = wait_for(server_pack, started["id"])
    assert run["status"] == "done", run["events"][-1]
    events = run["events"]
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["fetch", "scan", "tune", "port", "pr"]  # no capture
    assert any("eval pack" in e.get("message", "") and "4 hand-labelled cases" in e["message"] for e in events if e["type"] == "note")
    assert next(e for e in events if e["type"] == "ported")["sites"][0]["status"] == "ported"


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
