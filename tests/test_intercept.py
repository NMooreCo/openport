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


def test_a_case_cant_reach_the_closed_providers_themselves():
    """A call OpenPort can't route (the Responses API, a beta method) must fail, not be answered by GPT or Claude and
    graded as Nemotron on the user's real key."""
    import httpx
    import pytest

    sent = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: sent.append(str(request.url)) or httpx.Response(200, json={})))
    with intercept.routed(object()):
        with pytest.raises(intercept.ProviderBlocked, match="api.openai.com"):
            client.post("https://api.openai.com/v1/responses", json={})
        with pytest.raises(intercept.ProviderBlocked, match="api.anthropic.com"):
            client.post("https://api.anthropic.com/v1/messages?beta=true", json={})
        client.get("https://api.tokenfactory.nebius.com/v1/models")  # Token Factory itself is fine
    client.post("https://api.openai.com/v1/responses", json={})  # outside a case nothing is refused
    assert sent == ["https://api.tokenfactory.nebius.com/v1/models", "https://api.openai.com/v1/responses"]


def _recording(sent: list):
    """An httpx2 transport (what the OpenAI and Anthropic SDKs send with) that keeps each URL it's asked for."""
    import httpx2

    return httpx2.MockTransport(lambda request: sent.append(str(request.url)) or httpx2.Response(404, json={}))


def test_the_sdks_own_calls_that_arent_routed_never_leave():
    """The SDKs send through httpx2, not httpx: a Responses call or a token count on the real SDKs stops here."""
    import anthropic
    import openai
    import pytest

    sent = []
    gpt = openai.OpenAI(api_key="sk-real", max_retries=0, http_client=openai.DefaultHttpxClient(transport=_recording(sent)))
    claude = anthropic.Anthropic(api_key="sk-ant-real", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=_recording(sent)))
    with intercept.routed(object()):
        with pytest.raises(intercept.ProviderBlocked, match="api.openai.com"):  # not wrapped: the case's error says why
            gpt.responses.create(model="gpt-5-mini", input="hello")
        with pytest.raises(anthropic.APIConnectionError) as raised:  # Anthropic's SDK wraps it; the runner looks behind
            claude.messages.count_tokens(model="claude-sonnet-5-5", messages=[{"role": "user", "content": "hello"}])
        assert isinstance(raised.value.__cause__, intercept.ProviderBlocked)
    assert sent == []
    with pytest.raises(openai.NotFoundError):  # outside a case the same client goes out as it would
        gpt.responses.create(model="gpt-5-mini", input="hello")
    assert sent == ["https://api.openai.com/v1/responses"]


def test_the_async_sdks_calls_never_leave_either():
    import asyncio

    import openai
    import pytest

    sent = []
    gpt = openai.AsyncOpenAI(api_key="sk-real", max_retries=0, http_client=openai.DefaultAsyncHttpxClient(transport=_recording(sent)))

    async def ask():
        with intercept.routed(object()):
            await gpt.embeddings.create(model="text-embedding-3-small", input="hello")

    with pytest.raises(intercept.ProviderBlocked):
        asyncio.run(ask())
    assert sent == []


def test_a_thread_the_app_starts_during_a_case_cant_reach_them():
    """The route lives in the case's context, which a new thread doesn't get; the refusal is for the whole process."""
    import threading

    import openai

    sent, errors = [], []
    gpt = openai.OpenAI(api_key="sk-real", max_retries=0, http_client=openai.DefaultHttpxClient(transport=_recording(sent)))

    def work():
        try:
            gpt.chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": "hello"}])
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    with intercept.routed(object()):
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
    assert sent == [] and [type(error) for error in errors] == [intercept.ProviderBlocked]


def test_every_provider_host_is_refused_and_a_redirect_to_one_too():
    import httpx2
    import pytest

    intercept.install_http()

    def answer(request):
        sent.append(str(request.url))
        if request.url.host == "short.example":
            return httpx2.Response(302, headers={"location": "https://api.openai.com/v1/responses"})
        return httpx2.Response(200, json={})

    sent = []
    client = httpx2.Client(transport=httpx2.MockTransport(answer), follow_redirects=True)
    with intercept.routed(object()):
        for url in ("https://api.openai.com./v1/responses", "https://eu.api.openai.com/v1/responses", "https://API.Anthropic.com/v1/messages",
                    "https://my-resource.openai.azure.com/openai/deployments/gpt/chat/completions"):
            with pytest.raises(intercept.ProviderBlocked):
                client.post(url, json={})
        with pytest.raises(intercept.ProviderBlocked):
            client.get("https://short.example/go")  # followed to api.openai.com: refused there
        client.get("https://notopenai.com/")  # only the providers' own domains
    assert sent == ["https://short.example/go", "https://notopenai.com/"]


def test_openports_own_forwarding_isnt_refused():
    """The swapped client sends each call on with OpenPort's own client, whatever host that client is set to."""
    import openai

    sent = []
    own = openai.OpenAI(api_key="sk-x", max_retries=0, http_client=openai.DefaultHttpxClient(transport=_recording(sent)))

    class Completions:
        @staticmethod
        def create(**kwargs):
            try:
                own.chat.completions.create(**kwargs)
            except openai.NotFoundError:
                return "forwarded"

    class Swapped:
        chat = type("Chat", (), {"completions": Completions})

    app = openai.OpenAI(api_key="sk-real", max_retries=0, http_client=openai.DefaultHttpxClient(transport=_recording([])))
    with intercept.routed(Swapped()):
        assert app.chat.completions.create(model="gpt-5-mini", messages=[]) == "forwarded"
    assert sent == ["https://api.openai.com/v1/chat/completions"]


RESPONSES_APP = '''
from anthropic import Anthropic
from openai import OpenAI


def classify(text):
    reply = OpenAI(max_retries=0).responses.create(model="gpt-5-mini", input=text)
    return {"label": reply.output_text}


def count(text):
    reply = Anthropic(max_retries=0).messages.count_tokens(model="claude-sonnet-5-5", messages=[{"role": "user", "content": text}])
    return {"label": str(reply.input_tokens)}
'''


def test_a_blocked_call_fails_its_case_and_says_why(tmp_path, monkeypatch):
    (tmp_path / "respapp").mkdir()
    (tmp_path / "respapp" / "__init__.py").write_text("")
    (tmp_path / "respapp" / "llm.py").write_text(RESPONSES_APP)
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "c1", "input": {"text": "hello"}, "expected": {"label": "greeting"}}) + "\n")
    sites = SITE.replace("interapp", "respapp")
    (tmp_path / "openport.evals.yaml").write_text(
        "target: respapp\nsource_root: .\ncall_sites:\n" + sites.format(id="classify", sdk="openai") + sites.format(id="count", sdk="anthropic"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = load_spec(tmp_path)
    for site, host in zip(spec.call_sites, ("api.openai.com", "api.anthropic.com")):
        result = run_site(spec, site, "nano", "nvidia/nano-test", client=FakeTokenFactory(lambda request: completion(content="x")))
        [case] = result.cases  # Anthropic's SDK says "Connection error."; the case says what happened
        assert not case.passed and case.error.startswith(f"ProviderBlocked: A test case tried to reach {host}"), case.error
    for name in ("respapp", "respapp.llm"):
        sys.modules.pop(name, None)


def test_evals_replace_real_provider_keys_with_placeholders(monkeypatch, tmp_path):
    from openport.evals.runner import prepare_imports
    from openport.evals.spec import Spec

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    prepare_imports(Spec(target="app", source_root=tmp_path, call_sites=[]))
    import os
    assert os.environ["OPENAI_API_KEY"] == os.environ["ANTHROPIC_API_KEY"] == "openport-placeholder"
