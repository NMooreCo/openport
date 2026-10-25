"""Tests for evals that route the SDKs' own calls: code that builds its client where it calls it can be tested too."""

import json
import sys

from openport.capture.draft import draft_site, runnable
from openport.evals import intercept
from openport.evals.runner import run_site
from openport.evals.spec import load_spec
from openport.scan import scan_repo

from .fakes import FakeTokenFactory, completion

APP = '''
import anthropic
from openai import AsyncOpenAI, OpenAI


def classify(text):
    client = OpenAI()  # built where it's used: nothing at import to swap
    reply = client.chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": text}])
    return {"label": reply.choices[0].message.content}


async def classify_async(text):
    async with AsyncOpenAI() as client:
        reply = await client.chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": text}])
    return {"label": reply.choices[0].message.content}


class Assistant:
    def __init__(self):
        self.claude = anthropic.Anthropic()


def ask(text):
    reply = Assistant().claude.messages.create(model="claude-sonnet-5-5", max_tokens=64, messages=[{"role": "user", "content": text}])
    return {"label": reply.content[0].text}
'''

SITE = "  - {{id: {id}, file: interapp/llm.py, module: interapp.llm, function: {id}, intercept: true, sdk: {sdk}, " \
       "original_model: m, output: json_in_text, dataset: cases.jsonl, grader: {{type: fields, fields: [label]}}}}\n"


def test_calls_through_clients_built_anywhere_are_routed_to_nemotron(tmp_path, monkeypatch):
    (tmp_path / "interapp").mkdir()
    (tmp_path / "interapp" / "__init__.py").write_text("")
    (tmp_path / "interapp" / "llm.py").write_text(APP)
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps({"id": f"c{i}", "input": {"text": word}, "expected": {"label": label}}) + "\n"
                                                  for i, (word, label) in enumerate([("hello", "greeting"), ("bye", "farewell")])))
    (tmp_path / "openport.evals.yaml").write_text(
        "target: interapp\nsource_root: .\ncall_sites:\n"
        + "".join(SITE.format(id=name, sdk=sdk) for name, sdk in (("classify", "openai"), ("classify_async", "openai"), ("ask", "anthropic"))))
    monkeypatch.setattr(sys, "path", list(sys.path))
    labels = {"hello": "greeting", "bye": "farewell"}
    fake = FakeTokenFactory(lambda request: completion(content=labels[request["messages"][-1]["content"]]))
    spec = load_spec(tmp_path)
    for site in spec.call_sites:  # a local client, an async one, and one on an object's attribute
        result = run_site(spec, site, "nano", "nvidia/nano-test", client=fake, concurrency=2)
        assert result.pass_rate == 1.0, (site.id, [case.error for case in result.cases])
    assert len(fake.requests) == 6 and {r["model"] for r in fake.requests} == {"nvidia/nano-test"}
    assert intercept.ROUTE.get() is None  # nothing is routed outside a case
    for name in ("interapp", "interapp.llm"):
        sys.modules.pop(name, None)


HTTP_APP = '''
import httpx
import requests

OPENAI = "https://api.openai.com/v1/chat/completions"


def classify(text):
    reply = requests.post(OPENAI, headers={"Authorization": "Bearer sk-x"}, json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": text}]})
    reply.raise_for_status()
    return {"label": reply.json()["choices"][0]["message"]["content"]}


def ask(text):
    reply = httpx.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": "sk-ant-x", "anthropic-version": "2023-06-01"},
                       json={"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": text}]})
    reply.raise_for_status()
    return {"label": reply.json()["content"][0]["text"]}
'''


def test_plain_http_calls_are_tested_through_the_stand_in(tmp_path, monkeypatch):
    (tmp_path / "httpapp").mkdir()
    (tmp_path / "httpapp" / "__init__.py").write_text("")
    (tmp_path / "httpapp" / "llm.py").write_text(HTTP_APP)
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "c1", "input": {"text": "hello"}, "expected": {"label": "greeting"}}) + "\n")
    site = "  - {{id: {id}, file: httpapp/llm.py, module: httpapp.llm, function: {id}, intercept: true, sdk: http, " \
           "original_model: m, output: json_in_text, dataset: cases.jsonl, grader: {{type: fields, fields: [label]}}}}\n"
    (tmp_path / "openport.evals.yaml").write_text("target: httpapp\nsource_root: .\ncall_sites:\n" + site.format(id="classify") + site.format(id="ask"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    fake = FakeTokenFactory(lambda request: completion(content="greeting"))
    spec = load_spec(tmp_path)
    for call_site in spec.call_sites:  # a hard-coded api.openai.com and api.anthropic.com: neither is reached
        result = run_site(spec, call_site, "nano", "nvidia/nano-test", client=fake)
        assert result.pass_rate == 1.0, (call_site.id, [case.error for case in result.cases])
    assert [r["model"] for r in fake.requests] == ["nvidia/nano-test"] * 2  # Anthropic's request, translated
    for name in ("httpapp", "httpapp.llm"):
        sys.modules.pop(name, None)


METHODS_APP = '''
from dataclasses import dataclass

from openai import OpenAI


class Labeller:
    def __init__(self, model="gpt-5-mini"):
        self.client = OpenAI()
        self.model = model
        self.seen = []

    def label(self, text):
        self.seen.append(text)  # state on the instance: each case gets an instance of its own
        reply = self.client.chat.completions.create(model=self.model, messages=[{"role": "user", "content": text}])
        return {"label": reply.choices[0].message.content, "calls": len(self.seen)}

    @staticmethod
    def quick(text):
        reply = OpenAI().chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": text}])
        return {"label": reply.choices[0].message.content, "calls": 1}


@dataclass
class Configured:
    api_key: str
    tone: str = "plain"

    def label(self, text):
        client = OpenAI(api_key=self.api_key)
        reply = client.chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": f"{self.tone}: {text}"}])
        return {"label": reply.choices[0].message.content, "calls": 1}
'''


def test_calls_in_class_methods_run_on_an_instance_built_for_each_case(tmp_path, monkeypatch):
    (tmp_path / "methods").mkdir()
    (tmp_path / "methods" / "__init__.py").write_text("")
    (tmp_path / "methods" / "app.py").write_text(METHODS_APP)
    findings = {f.function: f for f in scan_repo(tmp_path).findings}
    assert runnable(findings["Labeller.label"], tmp_path) == (True, "") and runnable(findings["Labeller.quick"], tmp_path) == (True, "")
    ok, why = runnable(findings["Configured.label"], tmp_path)
    assert not ok and "takes api_key to build" in why and "`init:`" in why  # tone has a default

    (tmp_path / "cases.jsonl").write_text("".join(json.dumps({"id": f"c{i}", "input": {"text": word}, "expected": {"label": "ok", "calls": 1}}) + "\n"
                                                  for i, word in enumerate(["hello", "bye", "again"])))
    site = "  - {{id: {id}, file: methods/app.py, module: methods.app, function: {function}, intercept: true, sdk: openai, " \
           "original_model: m, output: json_in_text, dataset: cases.jsonl, grader: {{type: fields, fields: [label, calls]}}{extra}}}\n"
    (tmp_path / "openport.evals.yaml").write_text("target: methods\nsource_root: .\ncall_sites:\n" + "".join(
        site.format(id=f"s{i}", function=function, extra=extra) for i, (function, extra) in enumerate([
            ("Labeller.label", ""), ("Labeller.quick", ""), ("Configured.label", ", init: {api_key: sk-test, tone: terse}")])))
    monkeypatch.setattr(sys, "path", list(sys.path))
    fake = FakeTokenFactory(lambda request: completion(content="ok"))
    spec = load_spec(tmp_path)
    for call_site in spec.call_sites:
        result = run_site(spec, call_site, "nano", "nvidia/nano-test", client=fake, concurrency=3)
        assert result.pass_rate == 1.0, (call_site.function, [case.error for case in result.cases])
    assert {r["messages"][0]["content"] for r in fake.requests[-3:]} == {"terse: hello", "terse: bye", "terse: again"}  # init's arguments
    for name in ("methods", "methods.app"):
        sys.modules.pop(name, None)


def test_capture_drafts_a_routed_eval_when_no_client_can_be_swapped(tmp_path):
    (tmp_path / "interapp").mkdir()
    (tmp_path / "interapp" / "llm.py").write_text(APP)
    findings = {f.function: f for f in scan_repo(tmp_path).findings}
    local = findings["classify"]
    assert local.receiver_kind == "local" and runnable(local) == (True, "")
    draft = draft_site(local)
    assert draft["intercept"] is True and "client_attr" not in draft
    ask = findings["ask"]  # the client is on an object's attribute
    assert ask.receiver_kind == "attribute" and runnable(ask) == (True, "") and draft_site(ask)["intercept"] is True
