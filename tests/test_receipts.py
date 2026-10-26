"""The receipt reader, OpenPort's demo of a call that sends images: its eval set is sound, the scan sees the image,
the plan sends it to the Nemotron that reads images, and the eval runs the app's real code with the image in the
request."""

import base64
import json
import re
from pathlib import Path

import pytest

from openport import tokenfactory
from openport.evals.runner import run_site
from openport.evals.spec import load_spec
from openport.port.plan import build_plan
from openport.scan import scan_repo
from openport.tune import _findings, tiers_for

from .fakes import FakeTokenFactory, completion

RECEIPTS = Path(__file__).parents[1] / "examples" / "receipts"
SPEC = load_spec(RECEIPTS)
READ = SPEC.call_sites[0]
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test", "vision": "nvidia/vl-test"}


@pytest.fixture(autouse=True)
def no_pricing_lookup(monkeypatch):
    monkeypatch.setattr(tokenfactory, "catalog", lambda: {})


def test_every_receipt_has_an_image_and_a_label():
    cases = READ.cases()
    assert len(cases) == 24 and len({c["input"]["receipt"] for c in cases}) == 24
    for case in cases:
        image = RECEIPTS / "receipts" / "inbox" / case["input"]["receipt"]
        assert image.read_bytes().startswith(b"\x89PNG"), case["id"]
        label = case["expected"]
        assert re.fullmatch(r"FH-\d{6}", label["order_id"]) and re.fullmatch(r"2026-\d\d-\d\d", label["date"])
        assert isinstance(label["total"], float) and label["total"] > 0


def test_the_scan_sees_the_image_and_the_plan_sends_it_to_the_vision_model():
    finding, = scan_repo(RECEIPTS).portable()
    assert {"vision", "json_mode"} <= set(finding.features) and finding.model == "gpt-4o-mini"
    assert build_plan([finding], MODELS)[finding.id].model == "nvidia/vl-test"
    assert tiers_for(READ, _findings(SPEC), ("nano", "super", "ultra"), MODELS) == ("vision",)


def test_the_eval_runs_the_app_with_the_image_in_the_request():
    labels = {base64.b64encode((RECEIPTS / "receipts" / "inbox" / c["input"]["receipt"]).read_bytes()).decode(): c["expected"]
              for c in READ.cases()}

    def reads_the_receipt(request):  # a stand-in that "reads" the receipt it was sent, wrong on one
        image = next(p for p in request["messages"][0]["content"] if p["type"] == "image_url")["image_url"]["url"]
        label = dict(labels[image.removeprefix("data:image/png;base64,")])
        if label["order_id"] == "FH-106411":
            label["total"] = 282.00  # the subtotal, not what was paid
        return completion(content=json.dumps(label))

    fake = FakeTokenFactory(reads_the_receipt)
    result = run_site(SPEC, READ, "vision", "nvidia/vl-test", client=fake, limit=4)
    assert [case.passed for case in result.cases] == [False, True, True, True]
    assert result.cases[0].failures == ["total: got 282.0"]
    assert {r["model"] for r in fake.requests} == {"nvidia/vl-test"} and all(r["response_format"] == {"type": "json_object"} for r in fake.requests)
