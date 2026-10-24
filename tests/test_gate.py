import json
import sys
from pathlib import Path

import pytest
import yaml

from openport import tokenfactory
from openport.check import run_gate
from openport.evals.runner import run_site
from openport.evals.spec import load_spec
from openport.port import build_plan, port_repo
from openport.scan import scan_repo

from .fakes import FakeTokenFactory, completion

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}
TRIAGE_ANSWERS = {
    json.loads(line)["input"]["ticket"]: json.loads(line)["expected"]
    for line in (SUPPORTDESK / "evals/triage.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
}


@pytest.fixture(autouse=True)
def no_pricing_lookup(monkeypatch):
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})


@pytest.fixture
def ported_supportdesk(tmp_path, monkeypatch):
    """A ported copy of SupportDesk, importable in place of the original for the length of a test."""
    scan = scan_repo(SUPPORTDESK)
    plan = build_plan(scan.findings, MODELS, "super", {"triage.triage_ticket": "nano"})
    result = port_repo(SUPPORTDESK, scan, plan, tmp_path / "out")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in [m for m in sys.modules if m == "supportdesk" or m.startswith("supportdesk.")]:
        monkeypatch.delitem(sys.modules, name)
    yield result.output
    for name in [m for m in sys.modules if m == "supportdesk" or m.startswith("supportdesk.")]:
        del sys.modules[name]


def test_port_adds_the_gate(ported_supportdesk):
    summary = yaml.safe_load((ported_supportdesk / "openport.yaml").read_text())
    assert summary["gate"] == {"evals": "evals/openport.evals.yaml", "min_pass_rate": 0.9}
    action = (ported_supportdesk / ".github/workflows/openport.yml").read_text()
    assert "uv pip install -r pyproject.toml" in action
    assert "NEBIUS_API_KEY: ${{ secrets.NEBIUS_API_KEY }}" in action
    assert action.strip().endswith(".venv/bin/openport check .")


def test_gate_runs_the_models_the_code_names(ported_supportdesk):
    def answer(correct):
        def script(request):
            expected = TRIAGE_ANSWERS[request["messages"][-1]["content"]]
            return completion(content=json.dumps(expected if correct else {**expected, "priority": "low"}))
        return FakeTokenFactory(script)

    fake = answer(correct=True)
    (result,) = run_gate(ported_supportdesk, sites=["triage"], use_judge=False, client=fake)
    assert result.ok and result.site.pass_rate == 1.0
    assert {r["model"] for r in fake.requests} == {"nvidia/nano-test"}  # the model written in the ported code

    (failing,) = run_gate(ported_supportdesk, sites=["triage"], use_judge=False, client=answer(correct=False))
    assert not failing.ok and failing.required == 0.9


FACTORY_APP = {
    "gateapp/__init__.py": "",
    "gateapp/clients.py": '''from anthropic import Anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = Anthropic()
    return _client
''',
    "gateapp/label.py": '''import json

from gateapp.clients import get_client


def label(text):
    response = get_client().messages.create(
        model="claude-opus-4-5", max_tokens=100, system="Label it.", messages=[{"role": "user", "content": text}]
    )
    return json.loads(response.content[0].text)
''',
    "evals/openport.evals.yaml": '''target: gateapp
source_root: ..
call_sites:
  - id: label
    title: Labeller
    file: gateapp/label.py
    module: gateapp.label
    function: label
    client_attr: get_client()
    sdk: anthropic
    original_model: claude-opus-4-5
    dataset: label.jsonl
    grader: {type: fields, fields: [label]}
''',
    "evals/label.jsonl": '{"id": "a", "input": {"text": "hello"}, "expected": {"label": "greeting"}}\n'
                         '{"id": "b", "input": {"text": "bye"}, "expected": {"label": "farewell"}}\n',
}


def test_evals_can_swap_a_factory(tmp_path, monkeypatch):
    for path, text in FACTORY_APP.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = load_spec(tmp_path)
    labels = {"hello": "greeting", "bye": "farewell"}
    fake = FakeTokenFactory(lambda request: completion(content=json.dumps({"label": labels[request["messages"][-1]["content"]]})))

    result = run_site(spec, spec.call_sites[0], "nano", "nvidia/nano-test", client=fake)
    assert result.pass_rate == 1.0
    assert {r["model"] for r in fake.requests} == {"nvidia/nano-test"}
    import gateapp.label

    assert gateapp.label.get_client.__name__ == "get_client"  # the original factory is back
