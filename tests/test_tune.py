import json
from pathlib import Path

import pytest

from openport import tokenfactory
from openport.evals.spec import load_spec
from openport.port.plan import build_plan
from openport.scan import scan_repo
from openport.tune import export_plan, split_cases, tune_site
from openport.tune.engineer import parse_variant
from openport.tune.variant import Variant, apply_variant

from .fakes import FakeTokenFactory, completion

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
SPEC = load_spec(SUPPORTDESK)
TRIAGE = next(s for s in SPEC.call_sites if s.id == "triage")
ANSWERS = {case["input"]["ticket"]: case["expected"] for case in TRIAGE.cases()}
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}


@pytest.fixture(autouse=True)
def no_pricing_lookup(monkeypatch):
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})


def triage_fake(correct_when):
    """Answers every triage ticket correctly when `correct_when(request)` holds; otherwise wraps the JSON in chatter."""

    def script(request):
        answer = json.dumps(ANSWERS[request["messages"][-1]["content"]])
        return completion(content=answer if correct_when(request) else f"Sure! Here's the classification: {answer}")

    return FakeTokenFactory(script)


class RecordingEngineer:
    def __init__(self, proposal: Variant):
        self.proposal = proposal
        self.seen_case_ids: list[str] = []

    def __call__(self, site, model, variant, sample, failures):
        self.seen_case_ids += [case["id"] for case, _ in failures]
        assert sample["messages"][0]["role"] == "system"  # the engineer gets the app's real prompt
        return self.proposal


TWO_PROMPTS_APP = '''import json

from openai import OpenAI

client = OpenAI(api_key="test")
PROMPTS = {"security": "Review the diff for security holes.", "correctness": "Review the diff for logic bugs."}


def review(mode, diff):
    messages = [{"role": "system", "content": PROMPTS[mode]}, {"role": "user", "content": diff}]
    return json.loads(client.chat.completions.create(model="gpt-5", messages=messages).choices[0].message.content)
'''


def test_engineer_sees_every_prompt_a_call_sends(tmp_path, monkeypatch):
    """One call, a prompt per pass (like preflight's review helper): the addendum lands in both, so the engineer sees both."""
    (tmp_path / "twoprompts.py").write_text(TWO_PROMPTS_APP)
    (tmp_path / "review.jsonl").write_text("".join(
        json.dumps({"id": f"c{i}", "input": {"mode": ("security", "correctness")[i % 2], "diff": f"+x = {i}"}, "expected": {"verdict": "ok"}}) + "\n"
        for i in range(8)
    ))
    (tmp_path / "openport.evals.yaml").write_text(
        "target: twoprompts\nsource_root: .\ncall_sites:\n"
        "  - {id: review, title: Diff review, file: twoprompts.py, module: twoprompts, function: review, client_attr: client,\n"
        "     sdk: openai, original_model: gpt-5, dataset: review.jsonl, grader: {type: fields, fields: [verdict]}}\n"
    )
    monkeypatch.setattr("sys.path", list(__import__("sys").path))
    spec = load_spec(tmp_path)
    calls = []

    def engineer(site, model, variant, sample, failures, **extra):
        calls.append(extra)
        return Variant(system_addendum="Answer with the JSON object only.")

    fake = FakeTokenFactory(lambda request: completion(content=json.dumps({"verdict": "flagged"})))
    tune_site(spec, spec.call_sites[0], MODELS, client=fake, engineer=engineer, tiers=("nano",), rounds=2)
    assert calls == [{"systems": ["Review the diff for security holes.", "Review the diff for logic bugs."]}]
    __import__("sys").modules.pop("twoprompts", None)


def test_engineer_prompt_shows_each_system_prompt():
    from openport.tune.engineer import describe_prompts

    assert describe_prompts(["Classify."]) == "The app's system prompt:\n<<<\nClassify.\n>>>"
    both = describe_prompts(["Review for security.", "Review for logic bugs."])
    assert "2 different system prompts" in both and "appended to every one of them" in both
    assert "System prompt 1:\n<<<\nReview for security.\n>>>" in both and "System prompt 2:\n<<<\nReview for logic bugs.\n>>>" in both


def test_apply_variant():
    request = {"messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}], "max_tokens": 600}
    tuned = apply_variant(Variant(reasoning=False, system_addendum="JSON only.", max_tokens=2000, temperature=0.2), request)
    assert tuned["messages"][0]["content"] == "Be brief.\n\nJSON only."
    assert tuned["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert (tuned["max_tokens"], tuned["temperature"]) == (2000, 0.2)
    assert request["messages"][0]["content"] == "Be brief."  # the original request is untouched

    no_system = apply_variant(Variant(system_addendum="JSON only."), {"messages": [{"role": "user", "content": "hi"}]})
    assert no_system["messages"][0] == {"role": "system", "content": "JSON only."}
    assert apply_variant(Variant(max_tokens=100), {"messages": [], "max_tokens": 4000})["max_tokens"] == 4000  # a floor


def test_split_is_stable_and_disjoint():
    cases = TRIAGE.cases()
    tune, held = split_cases(cases)
    assert {c["id"] for c in tune}.isdisjoint({c["id"] for c in held})
    assert len(tune) + len(held) == len(cases) and len(held) == round(len(cases) * 0.3)
    assert split_cases(cases) == (tune, held)


def test_engineer_fix_passes_on_nano():
    fake = triage_fake(lambda request: "MAGIC RULE" in request["messages"][0]["content"])
    engineer = RecordingEngineer(Variant(system_addendum="MAGIC RULE: reply with the JSON object only.", reasoning=False))
    result = tune_site(SPEC, TRIAGE, MODELS, client=fake, engineer=engineer)

    assert [(a.tier, a.round) for a in result.attempts] == [("nano", 0), ("nano", 1)]
    assert result.baseline.pass_rate == 0
    assert (result.chosen.tier, result.chosen.pass_rate, result.chosen.holdout_pass) == ("nano", 1.0, 1.0)
    # The engineer only ever saw failures from the tuning cases, never the held-out ones.
    _, held = split_cases(TRIAGE.cases())
    assert engineer.seen_case_ids and set(engineer.seen_case_ids).isdisjoint({c["id"] for c in held})

    plan = export_plan(SPEC, [result], 0.9)["call_sites"]["triage.triage_ticket"]
    assert plan["model"] == "nvidia/nano-test"
    assert plan["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert plan["system_prompts"] and plan["system_prompts"][0].startswith("You are the triage system")
    decision = build_plan(scan_repo(SUPPORTDESK).findings, MODELS, "super", {"triage.triage_ticket": plan})["triage.triage_ticket"]
    assert decision.system_addendum.startswith("MAGIC RULE") and decision.extra_body == plan["extra_body"]


def test_moves_up_a_tier_when_nano_cannot_pass():
    fake = triage_fake(lambda request: request["model"] == "nvidia/super-test")
    result = tune_site(SPEC, TRIAGE, MODELS, client=fake, engineer=RecordingEngineer(Variant(system_addendum="Try harder.")), rounds=2)
    assert [(a.tier, a.round) for a in result.attempts] == [("nano", 0), ("nano", 1), ("super", 0)]
    assert result.chosen.tier == "super"


def test_no_tier_passes():
    fake = triage_fake(lambda request: False)
    result = tune_site(SPEC, TRIAGE, MODELS, client=fake, engineer=RecordingEngineer(Variant()), rounds=1)
    assert result.chosen is None and len(result.attempts) == 3
    plan = export_plan(SPEC, [result], 0.9)["call_sites"]["triage.triage_ticket"]
    assert plan["model"] == "keep" and "no Nemotron tier reached 90%" in plan["reason"]


def test_every_sample_is_counted_but_cases_are_counted_once():
    import dataclasses

    sampled = dataclasses.replace(TRIAGE, samples=2)
    result = tune_site(SPEC, sampled, MODELS, client=triage_fake(lambda request: True), engineer=RecordingEngineer(Variant()))
    n = len(TRIAGE.cases())
    assert (result.chosen.total, result.chosen.cases, result.chosen.pass_rate) == (2 * n, n, 1.0)


def test_a_tier_can_be_served_somewhere_else():
    """A fine-tuned student on its own endpoint: its tier uses its own client, the others use Token Factory."""
    token_factory = triage_fake(lambda request: False)
    student = triage_fake(lambda request: True)
    result = tune_site(
        SPEC, TRIAGE, {**MODELS, "student": "ft:llama-3.1-8b:student"}, client=token_factory, engineer=RecordingEngineer(Variant()),
        tiers=("nano", "student", "super"), rounds=1, clients={"student": student},
    )
    assert [(a.tier, a.model) for a in result.attempts] == [("nano", "nvidia/nano-test"), ("student", "ft:llama-3.1-8b:student")]
    assert result.chosen.tier == "student" and {r["model"] for r in student.requests} == {"ft:llama-3.1-8b:student"}


def test_parse_variant_tolerates_chatter():
    text = 'Here you go:\n```json\n{"reasoning": false, "system_addendum": "JSON only.", "max_tokens": 0, "temperature": null, "rationale": "x"}\n```'
    variant = parse_variant(text, fallback=Variant())
    assert (variant.reasoning, variant.system_addendum, variant.max_tokens) == (False, "JSON only.", None)
    assert parse_variant("no json here", fallback=Variant(rationale="kept")).rationale == "kept"
