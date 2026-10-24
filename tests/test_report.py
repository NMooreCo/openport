from pathlib import Path

from openport import pricing, report
from openport.port import build_plan, port_repo
from openport.scan import scan_repo

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}

PAGE = {
    "url": "https://openai.com/api/pricing/",
    "content": "GPT-5 mini: Input $0.25 / 1M tokens, Cached input $0.025, Output $2.00 / 1M tokens.",
}


def test_price_is_accepted_only_if_the_page_says_so():
    assert pricing.verified({"input": 0.25, "output": 2.0, "source": PAGE["url"]}, [PAGE]) == (0.25, 2.0, PAGE["url"])
    # A number the page doesn't contain (an invented price) is rejected.
    assert pricing.verified({"input": 0.3, "output": 2.0, "source": PAGE["url"]}, [PAGE]) is None
    assert pricing.verified({"input": None, "output": 2.0}, [PAGE]) is None

    searched = []
    price = pricing.closed_model_price(
        "gpt-5-mini",
        search=lambda query, domains: searched.append(domains) or [PAGE],
        extract=lambda model, results: {"input": 0.25, "output": 2, "source": PAGE["url"]},
        use_cache=False,
    )
    assert (price.input_per_m, price.output_per_m, price.source) == (0.25, 2.0, PAGE["url"])
    assert searched == [pricing.PRICING_DOMAINS["openai"]]  # only the provider's own pages
    assert pricing.parse_manual("claude-opus-5-5=5/25").output_per_m == 25


def test_a_page_must_name_the_exact_model():
    assert pricing.mentions_model("claude-sonnet-5-5", "Claude Sonnet 5.5 The best combination $2 / MTok $10 / MTok")
    assert not pricing.mentions_model("claude-sonnet-5-5", "Claude Sonnet 5 $2 / MTok $10 / MTok")
    assert not pricing.mentions_model("claude-sonnet-5", "Claude Sonnet 5.5 $2 / MTok")
    assert pricing.mentions_model("gpt-5", "Quick comparison GPT-5 $1.25 GPT-5 Mini $0.25")
    assert not pricing.mentions_model("gpt-5", "Introducing GPT-5.5 and GPT-5-mini")
    assert pricing.mentions_model("gpt-5-mini", "GPT-5 mini: Input $0.25")
    assert pricing.mentions_model("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet $3 / MTok")

    # Sonnet 5's page has the right numbers but the wrong model: not accepted for Sonnet 5.5.
    sonnet_5_page = {"url": "https://www.anthropic.com/news/claude-sonnet-5", "content": "Claude Sonnet 5 costs $2 / $10 per million tokens."}
    extracted = {"input": 2, "output": 10, "source": sonnet_5_page["url"]}
    assert pricing.verified(extracted, [sonnet_5_page], "claude-sonnet-5-5") is None
    assert pricing.verified(extracted, [sonnet_5_page], "claude-sonnet-5") == (2.0, 10.0, sonnet_5_page["url"])


def test_price_lookup_retries_a_missed_read():
    reads = iter([{"input": 9.99, "output": 2, "source": PAGE["url"]}, {"input": 0.25, "output": 2, "source": PAGE["url"]}])
    searched = []
    price = pricing.closed_model_price(
        "gpt-5-mini",
        search=lambda query, domains: searched.append(query) or [PAGE],
        extract=lambda model, results: next(reads),
        use_cache=False,
    )
    assert price.input_per_m == 0.25
    assert len(searched) == 1  # the second read reused the first search

    misses = []
    assert pricing.closed_model_price(
        "gpt-5-mini",
        search=lambda query, domains: misses.append(query) or [PAGE],
        extract=lambda model, results: None,
        use_cache=False,
    ) is None
    assert len(misses) == 2  # gave up after three reads and one broader search


def test_pr_description(tmp_path):
    scan = scan_repo(SUPPORTDESK)
    assignments = {"triage.triage_ticket": {"model": "nano", "variant": {"reasoning": False, "system_addendum": "JSON only."}}}
    port_repo(SUPPORTDESK, scan, build_plan(scan.findings, MODELS, "super", assignments), tmp_path / "out")

    def attempt(tier, passed, variant=None, holdout=None):
        return {
            "tier": tier, "model": MODELS[tier], "round": 0, "variant": variant or {}, "tune_pass": passed / 10,
            "holdout_pass": holdout, "passed": passed, "total": 10, "cost_usd": 0.001, "failed_cases": [],
            "prompt_tokens": 4000, "completion_tokens": 1000,
        }

    chosen = attempt("nano", 10, {"reasoning": False, "system_addendum": "JSON only."}, holdout=1.0)
    tune = {
        "target_pass_rate": 0.9,
        "results": [{"site_id": "triage", "file": "supportdesk/triage.py", "function": "triage_ticket",
                     "attempts": [attempt("nano", 6), chosen], "chosen": chosen}],
    }
    prices = {
        "gpt-5-mini": pricing.Price("gpt-5-mini", 0.25, 2.0, PAGE["url"], "2026-10-24"),
        "nvidia/nano-test": pricing.Price("nvidia/nano-test", 0.06, 0.24, "Token Factory", "2026-10-24"),
    }
    rows = report.build_rows(report.load_summary(tmp_path / "out"), tune, prices.get)
    body = report.render("supportdesk", rows, tune)

    triage = next(r for r in rows if r.id == "triage.triage_ticket")
    assert (triage.plain, triage.after, triage.held_out) == (0.6, 1.0, 1.0)
    assert triage.per_call_tokens == (400, 100)
    # 1,000 calls of 400 in / 100 out tokens: $0.30 on gpt-5-mini, $0.048 on the Nano price.
    assert round(triage.cost_was, 4) == 0.3 and round(triage.cost_now, 4) == 0.048
    assert "| `triage.triage_ticket` ([supportdesk/triage.py:31](supportdesk/triage.py#L31)) | `gpt-5-mini` | `nvidia/nano-test` | 60% | 100% (10 cases) | 100% | $0.30 → $0.0480 |" in body
    assert "tuned for nano (reasoning off, +2 words of instructions): “JSON only.”" in body
    assert "held out" in body and "## Move LLM calls to NVIDIA Nemotron" in body
    # Calls without tuning data or prices don't get made-up numbers.
    assert "| `pii.extract_pii`" in body and "| `nvidia/super-test` | - | - | - | - |" in body

    # When the report knows how much of Nemotron's output was the reply itself, the original model isn't charged
    # for Nemotron's reasoning: 1,000 calls of 400 in / 30 reply tokens is $0.16 on gpt-5-mini. Nemotron still pays for all 100.
    chosen["answer_tokens"] = 300
    [triage] = [r for r in report.build_rows(report.load_summary(tmp_path / "out"), tune, prices.get) if r.id == "triage.triage_ticket"]
    assert round(triage.cost_was, 4) == 0.16 and round(triage.cost_now, 4) == 0.048
    assert "Nemotron's reasoning not counted" in report.render("supportdesk", [triage], tune)
