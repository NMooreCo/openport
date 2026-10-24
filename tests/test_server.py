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


def fake_token_factory():
    def script(request):
        system = request["messages"][0]["content"]
        if system.startswith("You tune LLM calls"):  # the engineer (Nemotron Ultra)
            return completion(content=json.dumps({"reasoning": False, "system_addendum": "Output the JSON object and nothing else.", "max_tokens": None, "temperature": None, "rationale": "chatter around JSON"}))
        answer = json.dumps({"label": LABELS[request["messages"][-1]["content"]]})
        if "nothing else" in system:
            return completion(content=answer)
        return completion(content=f"Here is the label: {answer}")  # the plain swap wraps its JSON in chatter

    return FakeTokenFactory(script)


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
