"""Run SupportDesk's real code through the eval runner against a scripted fake Token Factory."""

import json
import re
from pathlib import Path

import pytest

from openport import tokenfactory
from openport.evals.runner import run_site
from openport.evals.spec import load_spec

from .fakes import FakeTokenFactory, completion

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
SPEC = load_spec(SUPPORTDESK)
ORDERS = {o["order_id"]: o for o in json.loads((SUPPORTDESK / "supportdesk/data/orders.json").read_text(encoding="utf-8"))}
CATEGORIES = {"billing", "shipping", "returns", "product_question", "account", "technical", "other"}
PRIORITIES = {"urgent", "normal", "low"}
PII_TAGS = {"[NAME]", "[EMAIL]", "[PHONE]", "[ADDRESS]", "[CARD]"}


@pytest.fixture(autouse=True)
def no_pricing_lookup(monkeypatch):
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})


def site(site_id):
    return next(s for s in SPEC.call_sites if s.id == site_id)


def test_spec_and_datasets_are_well_formed():
    assert [s.id for s in SPEC.call_sites] == ["triage", "pii", "replies", "refunds", "policy"]
    for call_site in SPEC.call_sites:
        cases = call_site.cases()
        assert cases, call_site.id
        assert len({case["id"] for case in cases}) == len(cases)
        assert all({"id", "input", "expected"} <= case.keys() for case in cases)


def test_labels_are_internally_consistent():
    """Catch label slips in the hand-labelled sets: each expected value must be possible given the case and the app's data."""
    for case in site("triage").cases():
        assert case["expected"]["category"] in CATEGORIES and case["expected"]["priority"] in PRIORITIES, case["id"]

    for case in site("pii").cases():
        ticket, expected = case["input"]["ticket"], case["expected"]
        assert all(s in ticket for s in expected["pii_strings"] + expected["must_keep"]), case["id"]
        assert set(expected["tags"]) <= PII_TAGS, case["id"]
        assert expected["order_id"] is None or re.fullmatch(r"FH-\d{6}", expected["order_id"]), case["id"]

    for case in site("replies").cases():
        given, expected = case["input"], case["expected"]
        greeting = given["customer_name"].split()[0] if given.get("customer_name") else "Hi there"
        assert greeting in expected["must_include"], case["id"]
        # The app only promises to mention order numbers the customer gave.
        assert all(item in given["ticket"] for item in expected["must_include"] if item.startswith("FH-")), case["id"]

    for case in site("refunds").cases():
        order = ORDERS[re.search(r"FH-\d{6}", case["input"]["ticket"]).group(0)]
        expected = case["expected"]
        if expected["action"] == "refund":
            ceiling = sum(i["price"] for i in order["items"]) + order["shipping_cost"]
            assert 0 < expected["amount"] <= min(ceiling, 250), case["id"]  # agents can't go over $250 (s8)
        else:
            assert expected["amount"] is None, case["id"]
        if order["refunds_last_90_days"] >= 3:
            assert expected["action"] == "escalate", case["id"]  # repeat refunds go to a supervisor first (s10)

    for case in site("policy").cases():
        expected = case["expected"]
        assert expected["decision"] in {"approve", "partial", "deny"}, case["id"]
        assert (expected["decision"] == "deny") == (expected["refund_amount"] == 0), case["id"]


def test_triage_runs_through_swapped_openai_client():
    fake = FakeTokenFactory(lambda request: completion(content='{"category": "returns", "priority": "urgent"}'))
    result = run_site(SPEC, site("triage"), "nano", "nvidia/nemotron-nano", client=fake, limit=3)
    assert [case.passed for case in result.cases] == [True, False, False]
    assert {request["model"] for request in fake.requests} == {"nvidia/nemotron-nano"}
    assert result.prompt_tokens == 300


def test_pii_structured_output_path():
    payload = {
        "customer_name": "Maria Gonzalez", "email": "maria.g@outlook.com", "phone": "5035550142", "order_id": "FH-100233",
        "redacted_text": "Hi, this is [NAME]. My order FH-100233 still hasn't arrived. You can reach me at [EMAIL] or [PHONE].",
    }
    fake = FakeTokenFactory(lambda request: completion(content=json.dumps(payload)))
    result = run_site(SPEC, site("pii"), "nano", "m", client=fake, limit=1)
    assert result.cases[0].passed, result.cases[0].failures


def test_refund_agent_tool_loop():
    def script(request):
        if request["messages"][-1]["role"] == "user":
            return completion(tool_calls=[("c1", "lookup_order", {"order_id": "FH-100301"})])
        return completion(tool_calls=[("c2", "issue_refund", {"order_id": "FH-100301", "amount": 149, "reason": "s1"})])

    fake = FakeTokenFactory(script)
    result = run_site(SPEC, site("refunds"), "super", "m", client=fake, limit=2, concurrency=1)
    assert result.cases[0].passed  # refunds-01 expects a $149 refund
    assert not result.cases[1].passed  # refunds-02 expects a denial


def test_policy_runs_through_anthropic_translation():
    answer = "<decision>approve</decision>\n<refund_amount>$449.00</refund_amount>\n<brief>Within policy.</brief>"
    fake = FakeTokenFactory(lambda request: completion(content=answer, reasoning="Delivered 6 days ago..."))
    result = run_site(SPEC, site("policy"), "ultra", "m", client=fake, limit=1)
    assert result.cases[0].passed, result.cases[0]
    # Claude's extended thinking maps onto Nemotron's reasoning switch.
    assert fake.requests[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}


def test_crashes_are_recorded_per_case():
    fake = FakeTokenFactory(lambda request: completion(content="Sure! Here is the JSON: {oops"))
    result = run_site(SPEC, site("triage"), "nano", "m", client=fake, limit=2)
    assert result.error_types() == {"JSONDecodeError": 2}
