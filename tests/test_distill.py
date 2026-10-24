"""Tests for `openport distill`: training sets from Ultra's answers to new inputs, filtered by the call's own checks."""

import json
from pathlib import Path

import pytest

from openport import config, tokenfactory
from openport.distill import best_variant, distill_site, label_free_check
from openport.evals.spec import load_spec

from .fakes import FakeTokenFactory, completion

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
SPEC = load_spec(SUPPORTDESK)
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})


def site(site_id):
    return next(s for s in SPEC.call_sites if s.id == site_id)


def test_teacher_uses_its_best_tuned_settings():
    attempt = lambda tier, passed, addendum: {"tier": tier, "passed": passed, "total": 10, "variant": {"system_addendum": addendum}}
    report = {"results": [{"site_id": "replies", "attempts": [
        attempt("super", 9, "super fix"), attempt("ultra", 7, "weak"), attempt("ultra", 9, "best ultra fix"),
    ]}]}
    assert best_variant(report, "replies").system_addendum == "best ultra fix"
    assert best_variant(None, "replies").is_plain()


def test_free_text_answers_must_pass_the_format_rules_and_judge():
    replies = site("replies")
    good = "Hi there,\n\nThanks for writing in. The team will confirm the next steps shortly.\n\n— The Fernhill Support Team"
    assert label_free_check(replies, good, {"ticket": "x"}, judge=lambda rubric, case, output: 5) == (True, "")
    ok, reason = label_free_check(replies, good.replace("— The Fernhill Support Team", "Cheers!"), {"ticket": "x"}, judge=lambda *a: 5)
    assert not ok and reason == "wrong sign-off"
    assert not label_free_check(replies, good, {"ticket": "x"}, judge=lambda *a: 2)[0]  # the judge's bar still applies
    assert label_free_check(site("triage"), {"category": "billing"}, {}, judge=None) == (True, "")


def test_distill_triage_from_new_inputs_only():
    eval_ticket = site("triage").cases()[0]["input"]["ticket"]
    new_tickets = ["My promo code SAVE10 was rejected at checkout.", "Is the Ember 20 bag machine washable?"]

    def script(request):
        system = request["messages"][0]["content"]
        if system.startswith("You write test inputs"):  # Super inventing inputs: one of them repeats an eval case
            return completion(content=json.dumps([{"ticket": t} for t in [eval_ticket, *new_tickets]]))
        return completion(content='{"category": "billing", "priority": "normal"}')  # Ultra answering through the app

    result, path = distill_site(SUPPORTDESK, "triage", examples=2, client=FakeTokenFactory(script), concurrency=1)
    examples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert (result.invented, result.kept) == (2, 2)
    assert [e["messages"][1]["content"] for e in examples] == new_tickets  # the eval ticket was never used
    first = examples[0]["messages"]
    assert first[0]["role"] == "system" and first[0]["content"].startswith("You are the triage system")  # the app's own prompt
    assert first[-1] == {"role": "assistant", "content": '{"category": "billing", "priority": "normal"}'}
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["kept"] == 2


def test_tool_loops_are_not_distilled():
    with pytest.raises(ValueError, match="tool-calling"):
        distill_site(SUPPORTDESK, "refunds", examples=1, client=FakeTokenFactory(lambda r: completion(content="[]")))
