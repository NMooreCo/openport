"""TypeScript and JavaScript call sites in the eval harness: the app runs under Node, its SDK pointed at a local proxy."""

import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from openport.evals import node
from openport.evals.context import CURRENT_CASE
from openport.evals.runner import run_site
from openport.evals.spec import load_spec
from openport.tune import tune_site
from openport.tune.variant import Variant

from .fakes import FakeTokenFactory, completion

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}

# A JavaScript app calling the OpenAI API the way the SDK does, without needing the SDK installed.
CLASSIFY_JS = """export async function classify(text) {
  const response = await fetch(`${process.env.OPENAI_BASE_URL}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
    body: JSON.stringify({
      model: "gpt-5-mini",
      messages: [{ role: "system", content: "Label the message." }, { role: "user", content: text }],
      response_format: { type: "json_object" },
      store: false,
    }),
  });
  if (!response.ok) throw new Error(`API error ${response.status}`);
  const body = await response.json();
  return { ...JSON.parse(body.choices[0].message.content), sawKey: process.env.NEBIUS_API_KEY ?? null };
}
"""
SPEC = """target: jsapp
source_root: .
call_sites:
  - id: classify
    file: src/classify.mjs
    function: classify
    args: [text]
    sdk: openai
    original_model: gpt-5-mini
    output: json_object
    dataset: cases.jsonl
    grader: {type: fields, fields: [label, sawKey]}
"""


def post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_proxy_sends_requests_through_the_swapped_client():
    seen = []

    class Client:
        class chat:  # noqa: N801 (the SDK's shape)
            class completions:  # noqa: N801
                @staticmethod
                def create(**request):
                    seen.append((CURRENT_CASE.get(), request))
                    return completion(content='{"label": "billing"}')

    with node.EvalProxy(Client) as proxy:
        status, body = post(proxy.base_url("case 7") + "/chat/completions",
                            {"model": "gpt-5-mini", "messages": [], "max_tokens": 5, "chat_template_kwargs": {"enable_thinking": False}})
        assert status == 200 and body["choices"][0]["message"]["content"] == '{"label": "billing"}'
        case, request = seen[0]
        assert case == "case 7"  # the reply is tied to its case, for the tuner's notes
        assert request == {"model": "gpt-5-mini", "messages": [], "max_tokens": 5, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        assert post(proxy.base_url("c") + "/chat/completions", {"model": "m", "messages": [], "stream": True})[0] == 400
        assert post(proxy.base_url("c").replace("/v1", "/v1/embeddings"), {})[0] == 404


def test_proxy_passes_token_factorys_errors_on_as_they_came():
    """The Python SDK unwraps "error"; the app's SDK expects it, so the original body goes back."""
    import httpx
    from openai import APIStatusError

    body = {"error": {"type": "insufficient_quota", "message": "Payment Required: You have exhausted your budget."}}

    class Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**request):
                    response = httpx.Response(402, json=body, request=httpx.Request("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions"))
                    raise APIStatusError("Error code: 402", response=response, body=body["error"])

    with node.EvalProxy(Client) as proxy:
        assert post(proxy.base_url("c") + "/chat/completions", {"model": "m", "messages": []}) == (402, body)


def test_the_proxy_speaks_anthropics_messages_api_too():
    from openport.runtime.openport_llm import AnthropicOnNemotron

    fake = FakeTokenFactory(lambda request: completion(content="billing"))
    with node.EvalProxy(AnthropicOnNemotron("nvidia/nano-test", client=fake)) as proxy:
        status, body = post(proxy.base_url("c").removesuffix("/v1") + "/v1/messages",
                            {"model": "claude-sonnet-5-5", "max_tokens": 64, "system": "Label it.", "messages": [{"role": "user", "content": "refund"}]})
        assert status == 200 and (body["type"], body["role"], body["content"]) == ("message", "assistant", [{"text": "billing", "type": "text"}])
        assert fake.requests[0]["model"] == "nvidia/nano-test" and fake.requests[0]["messages"][0] == {"role": "system", "content": "Label it."}
        status, error = post(proxy.base_url("c").removesuffix("/v1") + "/v1/messages", {"model": "m", "max_tokens": 1, "messages": [], "stream": True})
        assert status == 400 and error["type"] == "error" and error["error"]["type"] == "api_error"  # in the shape Anthropic's SDK reads


def test_the_app_gets_no_secrets(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    env = node.clean_env("http://127.0.0.1:1/case/c/v1")
    assert env["NEBIUS_API_KEY"] == "openport-placeholder" and "GITHUB_TOKEN" not in env  # never the real key
    assert env["NEBIUS_BASE_URL"] == "http://127.0.0.1:1/case/c/v1"  # code OpenPort ported reaches the proxy too
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:1/case/c/v1" and env["OPENAI_API_KEY"] == "openport-placeholder"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1/case/c" and env["ANTHROPIC_API_KEY"] == "openport-placeholder"  # its SDK adds /v1


@needs_node
def test_typescript_loads_through_the_projects_tsx_or_type_stripping(tmp_path):
    assert node.node_command(tmp_path, "src/app.ts", "run", ["a"])[1:3] == ["--experimental-strip-types", "--no-warnings"]
    (tmp_path / "node_modules" / "tsx").mkdir(parents=True)
    assert node.node_command(tmp_path, "src/app.ts", "run", None)[1:3] == ["--import", "tsx"]
    assert node.node_command(tmp_path, "src/app.mjs", "run", None)[1] == str(node.RUNNER)  # plain JavaScript needs no loader


@needs_node
def test_a_javascript_call_site_runs_and_tunes_like_a_python_one(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "classify.mjs").write_text(CLASSIFY_JS)
    (tmp_path / "openport.evals.yaml").write_text(SPEC)
    labels = {"refund please": "billing", "app crashed": "technical", "love it": "praise", "where is my box": "shipping"}
    (tmp_path / "cases.jsonl").write_text("".join(
        json.dumps({"id": f"c{i}", "input": {"text": text}, "expected": {"label": label, "sawKey": "openport-placeholder"}}) + "\n"
        for i, (text, label) in enumerate(labels.items())
    ))
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-secret")  # held by OpenPort, never by the app
    monkeypatch.setattr(sys, "path", list(sys.path))

    def reply(request):
        text = request["messages"][-1]["content"]
        answer = json.dumps({"label": labels[text]})
        return completion(content=answer if "JSON only" in request["messages"][0]["content"] else f"Sure: {answer}")

    fake = FakeTokenFactory(reply)
    spec = load_spec(tmp_path)
    site = spec.call_sites[0]
    assert site.runs_on_node and site.module == "src/classify.mjs"

    plain = run_site(spec, site, "nano", "nvidia/nano-test", client=fake)
    assert plain.passed == 0 and plain.cases[0].error.startswith("NodeCaseError: SyntaxError")  # the app's own JSON.parse failed
    assert {r["model"] for r in fake.requests} == {"nvidia/nano-test"} and fake.requests[0]["store"] is False

    result = tune_site(spec, site, MODELS, client=fake, tiers=("nano",),
                       engineer=lambda *args, **extra: Variant(system_addendum="Reply with JSON only."))
    assert result.chosen is not None and result.chosen.tune_pass == 1.0
    assert result.system_prompts == ["Label the message."]  # what the port needs to find the prompt in the code


# The same, calling Anthropic's Messages API the way @anthropic-ai/sdk does.
CLAUDE_JS = """export async function classify(text) {
  const response = await fetch(`${process.env.ANTHROPIC_BASE_URL}/v1/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-api-key": process.env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01" },
    body: JSON.stringify({ model: "claude-sonnet-5-5", max_tokens: 64, system: "Label the message.", messages: [{ role: "user", content: text }] }),
  });
  if (!response.ok) throw new Error(`API error ${response.status}`);
  const body = await response.json();
  return { label: body.content[0].text };
}
"""


@needs_node
def test_a_javascript_call_on_anthropics_sdk_runs_on_nemotron(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "classify.mjs").write_text(CLAUDE_JS)
    (tmp_path / "openport.evals.yaml").write_text(SPEC.replace("sdk: openai", "sdk: anthropic").replace("fields: [label, sawKey]", "fields: [label]"))
    labels = {"refund please": "billing", "app crashed": "technical"}
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps({"id": f"c{i}", "input": {"text": text}, "expected": {"label": label}}) + "\n"
                                                  for i, (text, label) in enumerate(labels.items())))
    monkeypatch.setattr(sys, "path", list(sys.path))
    fake = FakeTokenFactory(lambda request: completion(content=labels[request["messages"][-1]["content"]]))
    spec = load_spec(tmp_path)
    result = run_site(spec, spec.call_sites[0], "nano", "nvidia/nano-test", client=fake)
    assert result.pass_rate == 1.0, [case.error for case in result.cases]
    assert {r["model"] for r in fake.requests} == {"nvidia/nano-test"}  # translated to chat completions on Nemotron


# Just enough of the `openai` package: the client posts to `${baseURL}/chat/completions`, as the SDK does.
OPENAI_STUB = {
    "node_modules/openai/package.json": '{"name": "openai", "type": "module", "exports": "./index.mjs"}',
    "node_modules/openai/index.mjs": """export default class OpenAI {
  constructor(options = {}) {
    const baseURL = (options.baseURL ?? process.env.OPENAI_BASE_URL).replace(/\\/$/, "");
    const apiKey = options.apiKey ?? process.env.OPENAI_API_KEY;
    this.chat = { completions: { create: async (body) => {
      const response = await fetch(`${baseURL}/chat/completions`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`API error ${response.status}`);
      return response.json();
    } } };
  }
}
export { OpenAI };
""",
}
APP_JS = """import OpenAI from "openai";

const client = new OpenAI();

export async function classify(text) {
  const reply = await client.chat.completions.create({ model: "gpt-5-mini", messages: [{ role: "user", content: text }] });
  return { label: reply.choices[0].message.content };
}
"""


@needs_node
def test_ported_typescript_still_passes_its_evals(tmp_path, monkeypatch):
    """`openport check` and the CI gate run the ported code: it has to reach OpenPort's proxy, not Token Factory without a key."""
    from openport.port import build_plan, port_repo
    from openport.scan import scan_repo

    root = tmp_path / "app"
    spec_text = SPEC.replace("fields: [label, sawKey]", "fields: [label]")
    files = {**OPENAI_STUB, "src/classify.mjs": APP_JS, "openport.evals.yaml": spec_text,
             "cases.jsonl": json.dumps({"id": "c1", "input": {"text": "refund please"}, "expected": {"label": "billing"}}) + "\n"}
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    report = scan_repo(root)
    port_repo(root, report, build_plan(report.findings, MODELS, "super"), tmp_path / "out")
    ported = (tmp_path / "out" / "src" / "classify.mjs").read_text()
    assert "process.env.NEBIUS_BASE_URL" in ported and '"nvidia/super-test"' in ported
    shutil.copytree(root / "node_modules", tmp_path / "out" / "node_modules")  # as `npm install` would
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = load_spec(tmp_path / "out")
    fake = FakeTokenFactory(lambda request: completion(content="billing"))
    gate = run_site(spec, spec.call_sites[0], "as written", None, client=fake)
    assert gate.pass_rate == 1.0, [case.error for case in gate.cases]
    assert fake.requests[0]["model"] == "nvidia/super-test"  # the model the ported code names


ANTHROPIC_STUB = {  # only imported: the ported code calls OpenPort's runtime instead
    "node_modules/@anthropic-ai/sdk/package.json": '{"name": "@anthropic-ai/sdk", "type": "module", "exports": "./index.mjs"}',
    "node_modules/@anthropic-ai/sdk/index.mjs": "export default class Anthropic {}\nexport { Anthropic };\n",
}
CLAUDE_APP_JS = """import Anthropic from "@anthropic-ai/sdk";

const claude = new Anthropic();

export async function classify(text) {
  const message = await claude.messages.create({ model: "claude-sonnet-5-5", max_tokens: 64, system: "Label the message.", messages: [{ role: "user", content: text }] });
  return { label: message.content[0].text };
}
"""


@needs_node
def test_javascript_ported_off_anthropics_sdk_passes_its_evals(tmp_path, monkeypatch):
    from openport.port import build_plan, port_repo
    from openport.scan import scan_repo

    root = tmp_path / "app"
    spec_text = SPEC.replace("sdk: openai", "sdk: anthropic").replace("fields: [label, sawKey]", "fields: [label]")
    files = {**ANTHROPIC_STUB, "src/classify.mjs": CLAUDE_APP_JS, "openport.evals.yaml": spec_text,
             "cases.jsonl": json.dumps({"id": "c1", "input": {"text": "refund please"}, "expected": {"label": "billing"}}) + "\n"}
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    report = scan_repo(root)
    port_repo(root, report, build_plan(report.findings, MODELS, "super"), tmp_path / "out")
    assert 'import { AnthropicOnNemotron } from "./openport_llm.mjs";' in (tmp_path / "out" / "src" / "classify.mjs").read_text()
    shutil.copytree(root / "node_modules", tmp_path / "out" / "node_modules")
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = load_spec(tmp_path / "out")
    fake = FakeTokenFactory(lambda request: completion(content="billing"))
    gate = run_site(spec, spec.call_sites[0], "as written", None, client=fake)
    assert gate.pass_rate == 1.0, [case.error for case in gate.cases]
    request = fake.requests[0]  # the runtime's translation, as Token Factory receives it
    assert request["model"] == "nvidia/super-test" and request["messages"][0] == {"role": "system", "content": "Label the message."}


@needs_node
def test_the_javascript_runtime_translates_like_the_python_one(tmp_path):
    import subprocess

    from openport.port.ts_rewrite import RUNTIME
    from openport.runtime.openport_llm import anthropic_to_openai

    params = {
        "model": "m", "max_tokens": 256, "system": [{"type": "text", "text": "Be brief."}], "temperature": 0.2,
        "tools": [{"name": "lookup", "description": "Find an order", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}],
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True}, "stop_sequences": ["END"], "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Where is "}, {"type": "text", "text": "order 7?"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Checking."}, {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"id": "7"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "shipped"}]},
                                         {"type": "image", "source": {"type": "url", "url": "https://example.com/box.png"}}]},
        ],
    }
    script = tmp_path / "translate.mjs"
    script.write_text(f"import {{ anthropicToOpenAI }} from {json.dumps(RUNTIME.as_uri())};\n"
                      f"process.stdout.write(JSON.stringify(anthropicToOpenAI({json.dumps(params)})));\n")
    javascript = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    python = anthropic_to_openai(**params)
    python.update(python.pop("extra_body"))  # Python passes the reasoning switch through the SDK's extra_body; on the wire it's top-level
    for request in (javascript, python):  # the same JSON, spaced differently by json.dumps and JSON.stringify
        for call in (call for message in request["messages"] for call in message.get("tool_calls") or []):
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
    assert javascript == python


SUPPORTDESK_TS = Path(__file__).parents[1] / "examples" / "supportdesk-ts"
needs_ts_example = pytest.mark.skipif(
    shutil.which("node") is None or not (SUPPORTDESK_TS / "node_modules" / "openai").is_dir(),
    reason="needs Node.js and `npm install` in examples/supportdesk-ts",
)


def _cases(site_id: str) -> list[dict]:
    path = SUPPORTDESK_TS.parent / "supportdesk" / "evals" / f"{site_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@needs_ts_example
def test_typescript_supportdesk_runs_the_python_apps_cases(monkeypatch):
    """The real TypeScript app, through tsx and the OpenAI Node SDK, graded on SupportDesk's own triage cases."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    answers = {case["input"]["ticket"]: case["expected"] for case in _cases("triage")}
    fake = FakeTokenFactory(lambda request: completion(content=json.dumps(answers[request["messages"][-1]["content"]])))
    spec = load_spec(SUPPORTDESK_TS)
    triage = next(s for s in spec.call_sites if s.id == "triage")
    result = run_site(spec, triage, "nano", "nvidia/nano-test", client=fake, limit=6)
    assert [c.error for c in result.cases] == [None] * 6 and result.passed == 6
    request = fake.requests[0]
    assert request["model"] == "nvidia/nano-test" and request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["content"].startswith("You are the triage system for Fernhill Supply Co.")


@needs_ts_example
def test_typescript_refund_agent_runs_its_tool_loop(monkeypatch):
    """The TypeScript agent looks up the order and takes one final action, through the proxy, like the Python one."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    cases = _cases("refunds")[:4]
    expected = {case["input"]["ticket"]: case["expected"] for case in cases}
    finals = {"refund": "issue_refund", "deny": "deny_refund", "escalate": "escalate_to_human"}

    def agent(request):
        ticket = request["messages"][1]["content"]
        order_id = re.search(r"FH-?\d{6}", ticket, re.IGNORECASE)[0]
        if request["messages"][-1]["role"] != "tool":
            return completion(tool_calls=[("call_1", "lookup_order", {"order_id": order_id})])
        assert json.loads(request["messages"][-1]["content"])["order_id"]  # the TypeScript store answered the lookup
        want = expected[ticket]
        args = {"order_id": order_id, "reason": "per policy"} | ({"amount": want["amount"]} if want["action"] == "refund" else {})
        return completion(tool_calls=[("call_2", finals[want["action"]], args)])

    fake = FakeTokenFactory(agent)
    spec = load_spec(SUPPORTDESK_TS)
    refunds = next(s for s in spec.call_sites if s.id == "refunds")
    result = run_site(spec, refunds, "nano", "nvidia/nano-test", client=fake, cases=cases)
    assert [c.error for c in result.cases] == [None] * 4 and result.passed == 4
    assert {r["model"] for r in fake.requests} == {"nvidia/nano-test"} and len(fake.requests) == 8  # lookup, then the decision
