"""`openport smoke` (what each Nemotron model supports) and `openport baseline` (the plain swap's score), offline."""

import json
from pathlib import Path
from types import SimpleNamespace

from openport import baseline, config, smoke, tokenfactory
from openport.evals.runner import CaseResult, SiteResult

from .fakes import FakeTokenFactory, completion

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"nano": "nvidia/nano", "super": "nvidia/super", "ultra": "nvidia/ultra", "vision": "nvidia/super-vl"}


def capable(request: dict):
    """A model that does everything the smoke checks ask, the way Token Factory answers when it works."""
    messages = request["messages"]
    if request.get("tools"):
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            return completion("It's out for delivery and arrives Tuesday.")
        return completion(tool_calls=[("call_1", "get_order_status", {"order_id": "FH-100233"})])
    if request.get("response_format") == {"type": "json_object"}:
        return completion('{"city": "Tokyo", "country": "Japan"}')
    if request.get("response_format") is not None:  # a Pydantic schema: chat.completions.parse
        return completion('{"name": "Jane Doe", "email": "jane.doe@example.com", "priority": "urgent"}')
    if isinstance(messages[0]["content"], list):  # the image check
        return completion("Red")
    return completion("pong")


def fake_client(script=capable) -> FakeTokenFactory:
    client = FakeTokenFactory(script)
    client.responses = SimpleNamespace(create=lambda **request: SimpleNamespace(output_text="pong", usage=SimpleNamespace(output_tokens=3)))
    return client


def test_every_smoke_check_passes_on_a_model_that_supports_it():
    checks = smoke._checks(fake_client(), "nvidia/super")
    results = {name: smoke._run("super", "nvidia/super", name, fn) for name, fn in checks.items()}
    assert {name for name, check in results.items() if not check.ok} == set()
    assert set(results) == {"chat", "reasoning off", "json_object", "json_schema", "tools (auto)", "tools (forced)",
                            "tool round-trip", "image input", "sampling settings", "responses API"}


def test_reasoning_off_sends_the_switch_and_forced_tools_name_the_function():
    client = fake_client()
    checks = smoke._checks(client, "nvidia/super")
    checks["reasoning off"]()
    checks["tools (forced)"]()
    assert client.requests[0]["extra_body"] == tokenfactory.reasoning(False)
    assert client.requests[1]["tool_choice"] == {"type": "function", "function": {"name": "get_order_status"}}


def test_a_check_that_fails_or_errors_is_a_result_not_a_crash():
    def broken(request):
        if request.get("tools"):
            return completion("I can't look that up.")  # no tool call
        raise TimeoutError("Token Factory took too long")

    checks = smoke._checks(fake_client(broken), "nvidia/nano")
    no_tool = smoke._run("nano", "nvidia/nano", "tools (auto)", checks["tools (auto)"])
    timeout = smoke._run("nano", "nvidia/nano", "chat", checks["chat"])
    assert not no_tool.ok and "no tool call" in no_tool.detail
    assert not timeout.ok and timeout.detail.startswith("TimeoutError: Token Factory took too long")


def test_smoke_runs_every_check_on_every_model(monkeypatch):
    monkeypatch.setattr(tokenfactory, "client", fake_client)
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    results = smoke.run_model_checks(concurrency=4)
    assert len(results) == len(MODELS) * 10
    assert {check.model for check in results} == set(MODELS.values())


def test_the_image_check_draws_a_valid_png():
    png = smoke.square_png(16)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")


def test_baseline_runs_each_call_on_each_tier_and_sends_images_to_the_vision_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "client", lambda: object())
    ran = []

    def run_site(spec, site, tier, model, **kwargs):
        ran.append((site.id, tier, model))
        cases = [CaseResult(f"c{i}", i < 3, 1.0 if i < 3 else 0.0, [], None, None, 0.1) for i in range(4)]
        return SiteResult(site.id, site.title, tier, model, cases, 100, 20, 0.001)

    monkeypatch.setattr(baseline, "run_site", run_site)
    seen = []
    results, path = baseline.run_baseline(ROOT / "examples" / "receipts", ["nano", "super"], use_judge=False, on_result=seen.append)
    assert ran == [("read", "vision", "nvidia/super-vl")]  # a call that sends images can't run on a text tier
    assert seen == results
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["kind"] == "baseline" and report["results"][0]["pass_rate"] == 0.75
    assert path.parent == tmp_path
