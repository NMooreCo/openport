"""Tests for `openport capture`: drafting an eval spec, importing logs, and generating cases."""

from pathlib import Path

import pytest

from openport.capture import capture_repo, write_capture
from openport.capture import draft, generate, logs
from openport.evals.spec import load_spec
from openport.runtime.openport_llm import UsageMeter
from openport.scan import scan_repo
from openport.scan.findings import CallSiteFinding

from .fakes import FakeTokenFactory, completion

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
EVALS = SUPPORTDESK / "evals"
LOG_FILES = {
    "triage.triage_ticket": EVALS / "triage.jsonl",
    "pii.extract_pii": EVALS / "pii.jsonl",
    "refunds.handle_refund_request": EVALS / "refunds.jsonl",
    "replies.draft_reply": EVALS / "replies.jsonl",
    "policy.review_case": EVALS / "policy.jsonl",
}


def finding(**kwargs) -> CallSiteFinding:
    base = dict(
        file="app/triage.py", line=1, language="python", function="triage_ticket",
        sdk="openai", api="chat.completions", receiver="client", receiver_kind="module_global",
        model="gpt-5-mini", output="json_object", module="app.triage", id="triage.triage_ticket",
    )
    base.update(kwargs)
    return CallSiteFinding(**base)


# --- draft -------------------------------------------------------------------

def test_humanize_title():
    assert draft.humanize_title(finding(function="handle_refund_request")) == "Handle refund request"
    assert draft.humanize_title(finding(function="<module>", file="app/triage.py")) == "Triage"


def test_client_attr_for():
    assert draft.client_attr_for(finding(receiver_kind="module_global", receiver="client")) == "client"
    assert draft.client_attr_for(finding(receiver_kind="imported", receiver="llm")) == "llm"
    assert draft.client_attr_for(finding(receiver_kind="factory", receiver="get_client")) == "get_client()"
    assert draft.client_attr_for(finding(receiver_kind="factory", receiver="get_client()")) == "get_client()"
    assert draft.client_attr_for(finding(receiver_kind="attribute", receiver="self.client")) is None
    assert draft.client_attr_for(finding(receiver_kind="local", receiver="c")) is None


def test_grader_for():
    assert draft.grader_for("text", None)["type"] == "text_rules"
    assert "judge" in draft.grader_for("text", None)
    assert draft.grader_for("json_object", ["a", "b"]) == {"type": "fields", "fields": ["a", "b"]}
    assert draft.grader_for("tools", None) == {"type": "fields", "fields": []}


def test_runnable():
    assert draft.runnable(finding())[0] is True
    assert draft.runnable(finding(language="typescript"))[0] is False
    assert draft.runnable(finding(module=None))[0] is False
    assert draft.runnable(finding(function="<module>"))[0] is False
    assert draft.runnable(finding(function="Agent.run"))[0] is False
    assert draft.runnable(finding(output="embedding"))[0] is False
    assert draft.runnable(finding(receiver_kind="local", receiver="c"))[0] is False


def test_param_names(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b=1, *, c): ...\n", encoding="utf-8")
    names = draft.param_names(tmp_path, finding(file="m.py", function="f"))
    assert names == ["a", "b", "c"]


def test_param_names_skips_self(tmp_path):
    (tmp_path / "m.py").write_text("class A:\n    def run(self, ticket): ...\n", encoding="utf-8")
    assert draft.param_names(tmp_path, finding(file="m.py", function="run")) == ["ticket"]


# --- logs --------------------------------------------------------------------

def test_load_log_cases_maps_output_to_expected(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"input": {"ticket": "x"}, "output": {"category": "billing"}}\n'
        '{"id": "keep", "input": {"ticket": "y"}, "expected": {"category": "returns"}}\n',
        encoding="utf-8",
    )
    cases = logs.load_log_cases(path, "triage")
    assert cases[0] == {"id": "triage-01", "input": {"ticket": "x"}, "expected": {"category": "billing"}}
    assert cases[1]["id"] == "keep"


def test_load_log_cases_rejects_bad_lines(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"input": {"a": 1}}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError):
        logs.load_log_cases(bad, "s")
    missing = tmp_path / "missing.jsonl"
    missing.write_text('{"output": {"a": 1}}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        logs.load_log_cases(missing, "s")


def test_infer_fields():
    cases = [
        {"expected": {"category": "a", "priority": "b", "extra": 1}},
        {"expected": {"category": "c", "priority": "d"}},
    ]
    assert logs.infer_fields(cases) == ["category", "priority"]
    assert logs.infer_fields([{"expected": "free text"}]) == []


# --- generate (pure helpers) -------------------------------------------------

def test_parse_inputs_filters_to_params():
    text = '[{"ticket": "a", "junk": 1}, {"ticket": "b"}]'
    assert generate.parse_inputs(text, ["ticket"]) == [{"ticket": "a"}, {"ticket": "b"}]


def test_parse_inputs_handles_fences_and_single_object():
    assert generate.parse_inputs('```json\n{"ticket": "a"}\n```', ["ticket"]) == [{"ticket": "a"}]
    assert generate.parse_inputs("not json", ["ticket"]) == []


def test_parse_label():
    assert generate.parse_label('```json\n{"category": "billing"}\n```') == {"category": "billing"}
    assert generate.parse_label("nope") is None


def test_input_prompt_mentions_params_and_count():
    prompt = generate.input_prompt("Triage", "triage_ticket", ["ticket"], "def triage_ticket(ticket): ...", 3)
    assert "ticket" in prompt and "triage_ticket" in prompt and "3" in prompt


# --- generate_cases with a scripted client -----------------------------------

def test_generate_cases_labels_json_output():
    def script(request):
        system = request["messages"][0]["content"]
        if system.startswith("You write test inputs"):
            return completion('[{"ticket": "a"}, {"ticket": "b"}]')
        return completion('{"category": "billing", "priority": "normal"}')

    cases = generate.generate_cases(
        "triage", "Triage", "triage_ticket", ["ticket"], "def triage_ticket(ticket): ...",
        n=2, needs_label=True, client=FakeTokenFactory(script), meter=UsageMeter(),
        super_model="super-x", ultra_model="ultra-x",
    )
    assert [c["input"] for c in cases] == [{"ticket": "a"}, {"ticket": "b"}]
    assert all(c["expected"] == {"category": "billing", "priority": "normal"} for c in cases)
    assert cases[0]["id"] == "triage-01"


def test_generate_cases_text_output_needs_no_label():
    fake = FakeTokenFactory(lambda request: completion('[{"ticket": "a"}]'))
    cases = generate.generate_cases(
        "replies", "Replies", "draft_reply", ["ticket"], "def draft_reply(ticket): ...",
        n=1, needs_label=False, client=fake, super_model="super-x", ultra_model="ultra-x",
    )
    assert cases == [{"id": "replies-01", "input": {"ticket": "a"}, "expected": {}}]
    assert len(fake.requests) == 1  # only the input generation call, no labelling


def test_generate_cases_uses_custom_labeller_not_ultra():
    fake = FakeTokenFactory(lambda request: completion('[{"text": "a"}, {"text": "b"}]'))
    cases = generate.generate_cases(
        "classify", "Classify", "classify", ["text"], "def classify(text): ...",
        n=2, needs_label=True, labeller=lambda inp: {"label": inp["text"].upper()},
        client=fake, super_model="super-x",
    )
    assert cases == [
        {"id": "classify-01", "input": {"text": "a"}, "expected": {"label": "A"}},
        {"id": "classify-02", "input": {"text": "b"}, "expected": {"label": "B"}},
    ]
    assert len(fake.requests) == 1  # input generation only; the labeller replaced the Ultra call


def test_original_labeller_runs_the_function(tmp_path, monkeypatch):
    (tmp_path / "labapp").mkdir()
    (tmp_path / "labapp/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "labapp/echo.py").write_text("def echo(x):\n    return {'v': x * 2}\n", encoding="utf-8")
    monkeypatch.setattr("sys.path", list(__import__("sys").path))
    label = generate.original_labeller(tmp_path, "labapp.echo", "echo")
    try:
        assert label({"x": 3}) == {"v": 6}
    finally:
        import sys as _sys
        _sys.modules.pop("labapp.echo", None)
        _sys.modules.pop("labapp", None)


def test_capture_label_original_needs_provider_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = scan_repo(SUPPORTDESK)
    with pytest.raises(Exception) as exc:
        capture_repo(SUPPORTDESK, report, generate=1, label="original")
    assert "OPENAI_API_KEY" in str(exc.value) or "ANTHROPIC_API_KEY" in str(exc.value)


# --- end to end on SupportDesk ----------------------------------------------

def test_capture_supportdesk_with_logs():
    report = scan_repo(SUPPORTDESK)
    log_map = {k: v for k, v in LOG_FILES.items()}
    result = capture_repo(SUPPORTDESK, report, logs=log_map)

    assert result.skipped == []  # every SupportDesk call is runnable
    by_id = {s.spec["id"]: s for s in result.sites}
    assert set(by_id) == set(LOG_FILES)
    assert by_id["triage.triage_ticket"].spec["grader"] == {"type": "fields", "fields": ["category", "priority"]}
    assert by_id["refunds.handle_refund_request"].spec["grader"] == {"type": "fields", "fields": ["action", "amount"]}
    assert by_id["replies.draft_reply"].spec["grader"]["type"] == "text_rules"
    assert len(by_id["triage.triage_ticket"].cases) == 15
    assert all(s.source == "logs" for s in result.sites)


def test_capture_bare_flags_missing_cases():
    report = scan_repo(SUPPORTDESK)
    result = capture_repo(SUPPORTDESK, report)
    assert all(len(s.cases) == 0 for s in result.sites)
    assert any("no cases" in note for note in result.sites[0].notes)


def test_write_capture_roundtrips_through_load_spec(tmp_path):
    report = scan_repo(SUPPORTDESK)
    result = capture_repo(SUPPORTDESK, report, logs=dict(LOG_FILES))
    paths = write_capture(result, tmp_path)

    assert paths["spec"].exists() and paths["notes"].exists()
    spec = load_spec(tmp_path)
    assert spec.target == "supportdesk"
    assert len(spec.call_sites) == 5
    triage = next(s for s in spec.call_sites if s.id == "triage.triage_ticket")
    assert len(triage.cases()) == 15
    assert spec.source_root.exists()
