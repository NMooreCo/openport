"""Tests for the Tavily research step in tuning: NVIDIA guidance in, cited sources out."""

import json

from openport import report
from openport.evals.runner import CaseResult
from openport.tune import export_plan, tune_site
from openport.tune.engineer import make_engineer
from openport.tune.research import Guidance, about_model, make_researcher, model_family, topic_for
from openport.tune.variant import Variant

from .fakes import FakeTokenFactory, completion
from .test_tune import MODELS, SPEC, TRIAGE, triage_fake

CARD = {"title": "NVIDIA Nemotron 3 Nano model card", "url": "https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "excerpt": "For structured output, turn reasoning off with enable_thinking=False."}


def failure(output=None, error=None):
    return ({"id": "c1", "input": {}, "expected": {}}, CaseResult("c1", False, 0.0, ["category: got 'x'"], output, error, 0.1))


def test_model_family_uses_nvidias_names():
    assert model_family("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B") == "NVIDIA Nemotron 3 Nano"
    assert model_family("nvidia/nemotron-3-super-120b-a12b") == "NVIDIA Nemotron 3 Super"
    assert model_family("nvidia/Nemotron-3_5-Lightning") == "NVIDIA Nemotron 3.5 Lightning"
    assert model_family("nvidia/Nemotron-3.5-Super-VL") == "NVIDIA Nemotron 3.5 Super VL"
    assert model_family("some/other-model") == "other-model"


def test_the_vision_models_sources_must_be_about_it_not_its_text_sibling():
    vl = "nvidia/Nemotron-3.5-Super-VL"
    assert about_model(vl, "Get Started with NVIDIA-Nemotron-3.5-Super-VL-120B-A12B — NVIDIA NIM")
    assert about_model(vl, "NVIDIA Nemotron 3.5 Super VL model card")
    assert not about_model(vl, "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8 · Hugging Face")
    assert not about_model(vl, "Nemotron 3.5 Super: reasoning settings")


def test_sources_must_be_about_the_exact_model():
    # Titles that came back in a live run, for the models they were (or weren't) about.
    nano, super_ = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/nemotron-3-super-120b-a12b"
    assert about_model(nano, "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 · Hugging Face")
    assert about_model(nano, "Nemotron Nano v3 (trained with this data) | 86.9")
    assert not about_model(nano, "nemotron-3-nano-omni-30b-a3b-reasoning Model by NVIDIA | NVIDIA NIM")
    assert about_model(super_, "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8 · Hugging Face")
    assert not about_model(super_, "Get Started with NVIDIA-Nemotron-3.5-Super-VL-120B-A12B — NVIDIA NIM")
    assert not about_model(super_, "Nemotron-3-Ultra | NVIDIA Dynamo Documentation")
    assert about_model("some/other-model", "anything")  # nothing to check against


def test_topic_follows_the_call_and_reasoning_leaks():
    assert topic_for("json_object", [failure()]) == "json"
    assert topic_for("tools", [failure()]) == "tools"
    assert topic_for("text", [failure()]) == "text"
    assert topic_for("json_object", [failure(output="<think>hmm</think>{")]) == "reasoning"


def test_researcher_searches_nvidia_pages_once_per_model_and_topic():
    searches = []

    def search(query, domains):
        searches.append((query, domains))
        return [{"title": CARD["title"], "url": CARD["url"], "content": "x " * 2000}, {"url": "https://docs.nvidia.com/empty", "content": ""}]

    research = make_researcher(search)
    first = research(TRIAGE, "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", [failure()])
    again = research(TRIAGE, "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", [failure()])
    assert first is again and len(searches) == 1  # cached for the rest of the run
    assert "NVIDIA Nemotron 3 Nano" in first.query and "huggingface.co" in searches[0][1]
    assert [s["url"] for s in first.sources] == [CARD["url"]]  # pages with no text are dropped
    assert len(first.sources[0]["excerpt"]) <= 700

    assert make_researcher(lambda query, domains: [])(TRIAGE, "nvidia/x", [failure()]) is None


QUOTE = "turn reasoning off with enable_thinking=False"


def test_engineer_gets_the_guidance_and_cites_only_what_the_page_says():
    proposal = {"reasoning": False, "system_addendum": "Reply with the JSON object only.", "max_tokens": None, "temperature": None,
                "rationale": "follow NVIDIA's advice", "sources": [
                    {"url": CARD["url"], "quote": QUOTE},
                    {"url": CARD["url"], "quote": "Always use temperature zero for JSON output"},  # the page doesn't say this
                    {"url": "https://example.com/made-up", "quote": QUOTE},  # a page it wasn't given
                    CARD["url"],  # no quote, so nothing to check
                ]}
    fake = FakeTokenFactory(lambda request: completion(content=json.dumps(proposal)))
    engineer = make_engineer(client=fake, model="nvidia/ultra-test")
    guidance = Guidance("nvidia/nano-test", "json", "q", [CARD])

    variant = engineer(TRIAGE, "nvidia/nano-test", Variant(), {"messages": [{"role": "system", "content": "Classify."}]}, [failure()], guidance=guidance)
    prompt = fake.requests[0]["messages"][1]["content"]
    assert CARD["excerpt"] in prompt and CARD["url"] in prompt
    assert variant.sources == [{"title": CARD["title"], "url": CARD["url"], "quote": QUOTE}]

    without = make_engineer(client=FakeTokenFactory(lambda request: completion(content=json.dumps(proposal))), model="nvidia/ultra-test")
    assert without(TRIAGE, "nvidia/nano-test", Variant(), None, [failure()]).sources == []  # nothing supplied, nothing cited


class GuidedEngineer:
    def __init__(self):
        self.guidance = []

    def __call__(self, site, model, variant, sample, failures, guidance=None, **extra):
        self.guidance.append(guidance)
        sources = [{"title": s["title"], "url": s["url"], "quote": QUOTE} for s in guidance.sources] if guidance else []
        return Variant(system_addendum="MAGIC RULE: reply with the JSON object only.", reasoning=False, sources=sources)


def test_tuning_records_research_and_carries_sources_into_the_plan():
    fake = triage_fake(lambda request: "MAGIC RULE" in request["messages"][0]["content"])
    engineer, seen = GuidedEngineer(), []
    result = tune_site(
        SPEC, TRIAGE, MODELS, client=fake, engineer=engineer,
        researcher=lambda site, model, failures: Guidance(model, "json", "q", [CARD]),
        on_research=lambda site, guidance: seen.append(guidance),
    )
    assert result.chosen.tier == "nano" and engineer.guidance[0].sources == [CARD]
    assert len(seen) == 1 and result.research == seen
    plan = export_plan(SPEC, [result], 0.9)["call_sites"]["triage.triage_ticket"]
    assert plan["variant"]["sources"] == [{"title": CARD["title"], "url": CARD["url"], "quote": QUOTE}]


def test_pr_quotes_the_guidance_a_change_followed():
    row = report.Row(
        id="triage.triage_ticket", file="app/triage.py", line=10, status="ported", was="gpt-5-mini", now="nvidia/nano-test",
        plain=0.5, after=1.0, held_out=1.0, cases=10, tier="nano", change="reasoning off", addendum="JSON only.",
        sources=[{"title": CARD["title"], "url": CARD["url"], "quote": QUOTE}],
    )
    body = report.render("app", [row], {"target_pass_rate": 0.9})
    escaped = QUOTE.replace("_", "\\_")  # shows as written on GitHub: enable_thinking, not emphasis
    assert f"  - Follows NVIDIA's guidance, [{CARD['title']}]({CARD['url']}): “{escaped}”" in body
    assert "checked word for word against the page" in body


def test_pr_leaves_out_citations_that_were_never_checked():
    """Plans from before quotes were required cite pages without saying what they relied on: not shown."""
    chosen = {"tier": "nano", "passed": 9, "total": 10, "holdout_pass": 1.0, "cases": 10, "prompt_tokens": 1000, "completion_tokens": 100,
              "variant": {"reasoning": False, "sources": [{"title": CARD["title"], "url": CARD["url"]}]}}
    tuning = {"file": "app/triage.py", "function": "triage_ticket", "attempts": [chosen], "chosen": chosen}
    site = {"id": "triage.triage_ticket", "file": "app/triage.py", "line": 10, "status": "ported", "was": "gpt-5-mini", "model": "nvidia/nano-test"}
    [row] = report.build_rows([site], {"results": [tuning]}, lambda model: None)
    assert row.sources == [] and "Follows NVIDIA" not in report.render("app", [row], {"target_pass_rate": 0.9})


def test_a_citation_counts_only_if_its_quote_is_on_the_page():
    from openport.citations import parse_citations, verified

    page = {"title": "Card", "url": "https://x/card", "excerpt": "For  structured output, turn “reasoning” off\nwith enable_thinking=False."}
    citations = parse_citations([
        {"url": "https://x/card", "quote": "turn \"Reasoning\" off with enable_thinking=False."},  # quotes, case and spacing differ
        {"url": "https://x/card", "quote": "structured output"},  # too short to show what was followed
        "https://x/card",
        {"quote": "no url"},
    ])
    assert [c["quote"] for c in citations] == ["turn \"Reasoning\" off with enable_thinking=False.", "structured output", ""]
    assert verified(citations, [page]) == [{"title": "Card", "url": "https://x/card", "quote": "turn \"Reasoning\" off with enable_thinking=False."}]
