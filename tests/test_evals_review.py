"""Evals, tuning and reports: cases from the Oct 29 code review, each a way a result could be wrong or lost."""

import json
import sys
import urllib.request
from types import SimpleNamespace

import pytest

from openport import report
from openport.capture.draft import client_attr_for
from openport.evals.node import EvalProxy
from openport.evals.runner import prepare_imports, run_site
from openport.evals.spec import Spec, load_spec
from openport.tune import export_plan, tune_site
from openport.tune.variant import Variant

from .fakes import FakeTokenFactory, completion

MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}


def finding(receiver: str, kind: str):
    return SimpleNamespace(receiver=receiver, receiver_kind=kind)


def test_only_a_plain_name_is_swapped_on_the_module():
    assert client_attr_for(finding("client", "module_global")) == "client"
    assert client_attr_for(finding("get_client", "factory")) == "get_client()"
    assert client_attr_for(finding("get_client()", "factory")) == "get_client()"
    # an expression can't be set on a module: the runner routes the SDK's calls instead of crashing on getattr
    assert client_attr_for(finding("openai.OpenAI()", "imported")) is None
    assert client_attr_for(finding("make('x')", "factory")) is None


def _app(tmp_path, monkeypatch, name: str, body: str, cases: int = 4):
    (tmp_path / f"{name}.py").write_text(body)
    (tmp_path / "cases.jsonl").write_text("".join(
        json.dumps({"id": f"c{i}", "input": {"text": f"t{i}"}, "expected": {"label": "ok"}}) + "\n" for i in range(cases)))
    (tmp_path / "openport.evals.yaml").write_text(
        f"target: {name}\nsource_root: .\ncall_sites:\n"
        f"  - {{id: label, title: Label, file: {name}.py, module: {name}, function: label, client_attr: client,\n"
        "     sdk: openai, original_model: gpt-5, dataset: cases.jsonl, grader: {type: fields, fields: [label]}}\n")
    monkeypatch.setattr("sys.path", list(sys.path))
    return load_spec(tmp_path)


LABEL_APP = '''import json

from openai import OpenAI

client = OpenAI(api_key="test")


def label(text):
    reply = client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": text}])
    return json.loads(reply.choices[0].message.content)
'''


def test_an_engineer_that_errors_doesnt_lose_the_run(tmp_path, monkeypatch):
    spec = _app(tmp_path, monkeypatch, "engineer_errors", LABEL_APP)

    def engineer(*args, **kwargs):
        raise TimeoutError("Token Factory took too long")

    def researcher(*args, **kwargs):
        raise RuntimeError("Tavily is out of credit")

    fake = FakeTokenFactory(lambda request: completion(content=json.dumps({"label": "wrong"})))
    result = tune_site(spec, spec.call_sites[0], MODELS, client=fake, engineer=engineer, researcher=researcher,
                       tiers=("nano", "super"), rounds=3)
    assert [a.tier for a in result.attempts] == ["nano", "super"]  # each tier still tried as it stood
    assert result.chosen is None
    sys.modules.pop("engineer_errors", None)


def test_a_call_that_cant_be_tested_stays_with_its_reason():
    tuning = SimpleNamespace(site_id="label", file="app.py", function="label", chosen=None, best=None, attempts=[],
                             error="ModuleNotFoundError: No module named 'torch'", system_prompts=[])
    spec = Spec(target="missing", source_root=__import__("pathlib").Path("does-not-exist"), call_sites=[])
    plan = export_plan(spec, [tuning], 0.9)["call_sites"]
    assert plan == {"label": {"model": "keep", "reason": "couldn't be tested: ModuleNotFoundError: No module named 'torch'"}}


def test_a_second_repo_with_the_same_module_name_gets_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.path", list(sys.path))
    answers = {}
    for repo, says in (("repo_a", "I am repo A"), ("repo_b", "I am repo B")):
        root = tmp_path / repo
        root.mkdir()
        (root / "shared_name_app.py").write_text(f"def who():\n    return {says!r}\n")
        prepare_imports(Spec(target=repo, source_root=root, call_sites=[]))
        answers[repo] = __import__("importlib").import_module("shared_name_app").who()
    assert answers == {"repo_a": "I am repo A", "repo_b": "I am repo B"}
    sys.modules.pop("shared_name_app", None)


def test_a_second_repo_cant_import_what_only_the_first_had(tmp_path, monkeypatch):
    """Neither a module only the first repo has (it's off the path) nor a folder it imported as a namespace package."""
    import importlib
    import importlib.util

    monkeypatch.setattr("sys.path", list(sys.path))
    first, second = tmp_path / "repo_first", tmp_path / "repo_second"
    (first / "nsapp").mkdir(parents=True)
    (first / "only_in_first.py").write_text("")
    (first / "nsapp" / "llm.py").write_text("WHO = 'first'\n")  # no __init__.py: a namespace package
    (second / "nsapp").mkdir(parents=True)
    (second / "nsapp" / "__init__.py").write_text("WHO = 'second'\n")
    prepare_imports(Spec(target="first", source_root=first, call_sites=[]))
    assert importlib.import_module("nsapp.llm").WHO == "first"
    prepare_imports(Spec(target="second", source_root=second, call_sites=[]))
    try:
        assert str(first.resolve()) not in sys.path
        assert importlib.util.find_spec("only_in_first") is None
        assert importlib.import_module("nsapp").WHO == "second"
    finally:
        for name in ("nsapp", "nsapp.llm", "only_in_first"):
            sys.modules.pop(name, None)


def test_the_eval_proxy_answers_requests_with_a_query_string():
    chat = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **request: completion("hi"))))
    messages = SimpleNamespace(messages=SimpleNamespace(create=lambda **request: SimpleNamespace(model_dump=lambda **k: {"ok": True})))
    with EvalProxy(chat, messages) as proxy:
        url = proxy.base_url("c1") + "/chat/completions?api-version=2024-10-21"
        request = urllib.request.Request(url, data=json.dumps({"model": "x", "messages": []}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200


def test_pr_rows_tell_same_named_methods_in_one_file_apart():
    results = [{"file": "agents.py", "function": "Summarizer.run", "chosen": {"pass_rate": 0.1}},
               {"file": "agents.py", "function": "Classifier.run", "chosen": {"pass_rate": 0.9}}]
    site = {"id": "agents.Classifier.run", "file": "agents.py"}
    assert report._tuning_for(site, results)["function"] == "Classifier.run"
    assert report._tuning_for({"id": "agents.run", "file": "agents.py"}, results)["function"] == "Summarizer.run"  # as before


def test_a_price_is_found_only_as_itself():
    from openport.pricing import _appears

    assert _appears(15, "Input: $15 / 1M tokens")
    assert _appears(15, "costs $15.")
    assert not _appears(15, "Input: $15.50 / 1M tokens")  # a neighbouring, different price
    assert _appears(2.5, "$2.50 per million")


def test_dataset_rows_without_an_id_or_with_a_number_get_a_text_id(tmp_path):
    (tmp_path / "rows.jsonl").write_text('{"input": {}, "expected": {}}\n{"id": 7, "input": {}, "expected": {}}\n')
    (tmp_path / "openport.evals.yaml").write_text(
        "target: t\nsource_root: .\ncall_sites:\n"
        "  - {id: s, title: S, file: a.py, module: a, function: f, client_attr: client, sdk: openai, original_model: gpt-5, dataset: rows.jsonl,\n"
        "     grader: {type: fields, fields: [x]}}\n")
    assert [case["id"] for case in load_spec(tmp_path).call_sites[0].cases()] == ["s-01", "7"]


def test_capture_cost_is_unknown_rather_than_a_crash_for_an_unpriced_model(monkeypatch):
    from openport import tokenfactory
    from openport.capture import _helper_cost
    from openport.runtime.openport_llm import UsageMeter

    meter = UsageMeter()
    meter.add("nvidia/new-model", 100, 20) if hasattr(meter, "add") else meter.by_model.setdefault(
        "nvidia/new-model", SimpleNamespace(prompt_tokens=100, completion_tokens=20))
    monkeypatch.setattr(tokenfactory, "model_info", lambda model: SimpleNamespace(cost=lambda p, c: None))
    assert _helper_cost(meter) is None


def test_a_call_whose_tuning_failed_partway_keeps_what_it_tried_and_spent(tmp_path, monkeypatch):
    """The first attempt ran (and cost money) before tuning failed: the report keeps it, and the cost counts."""
    from openport import config, tokenfactory, tune
    from openport.runtime.openport_llm import UsageMeter

    spec = _app(tmp_path, monkeypatch, "fails_partway", LABEL_APP)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(tokenfactory, "tiers", lambda: MODELS)
    monkeypatch.setattr(tokenfactory, "client", lambda: FakeTokenFactory(lambda request: completion(content=json.dumps({"label": "no"}))))
    monkeypatch.setattr(tokenfactory, "model_info", lambda model: SimpleNamespace(cost=lambda p, c: 0.25))
    helper = UsageMeter()

    def engineer_spends(client, meter=None):
        def engineer(*args, **kwargs):
            meter.record("nvidia/ultra-test", SimpleNamespace(prompt_tokens=1000, completion_tokens=100), 1.0)
            return Variant(system_addendum="Answer with JSON.")
        return engineer

    calls = []
    real_run_site = tune.run_site

    def run_site_then_fail(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError("Token Factory went away")
        return real_run_site(*args, **kwargs)

    monkeypatch.setattr(tune, "make_engineer", engineer_spends)
    monkeypatch.setattr(tune, "run_site", run_site_then_fail)
    seen = []
    results, totals, report_path, plan_path = tune.tune_spec(spec.source_root, tiers=("nano",), use_judge=False, use_research=False,
                                                            helper_meter=helper, on_attempt=lambda site, attempt: seen.append(attempt))
    [result] = results
    assert result.error == "ConnectionError: Token Factory went away"
    assert [a.tier for a in result.attempts] == ["nano"] and result.attempts == seen
    assert totals["helper_cost_usd"] == 0.25 and helper.by_model["nvidia/ultra-test"].calls == 1
    saved = json.loads(report_path.read_text(encoding="utf-8"))["results"][0]
    assert len(saved["attempts"]) == 1 and saved["error"].startswith("ConnectionError")
    assert list(json.loads(plan_path.read_text(encoding="utf-8"))["call_sites"].values()) == [
        {"model": "keep", "reason": "tuning stopped: ConnectionError: Token Factory went away"}]
    sys.modules.pop("fails_partway", None)


def test_helper_spend_counts_when_a_price_cant_be_read(monkeypatch):
    from openport import tokenfactory, tune
    from openport.runtime.openport_llm import UsageMeter

    meter = UsageMeter()
    for model in ("nvidia/ultra-test", "nvidia/unlisted"):
        meter.record(model, SimpleNamespace(prompt_tokens=10, completion_tokens=10), 1.0)

    def model_info(model):
        if model == "nvidia/unlisted":
            raise ConnectionError("catalog unreachable")
        return SimpleNamespace(cost=lambda p, c: 0.5)

    monkeypatch.setattr(tokenfactory, "model_info", model_info)
    assert tune.helper_spend(meter) == 0.5  # what's known still counts; the run doesn't fail over a price
