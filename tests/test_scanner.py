from pathlib import Path

from openport.scan import scan_repo

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"

PYTHON_FIXTURE = '''
import json

import anthropic
from openai import OpenAI
from twilio.rest import Client

MODEL = "claude-sonnet-4-5"
oai = OpenAI(base_url="https://example.test/v1")
sms = Client("sid", "token")
claude = anthropic.Anthropic()

PREFILLED = [
    {"role": "user", "content": "List three risks as JSON."},
    {"role": "assistant", "content": "["},
]


def risks():
    return claude.messages.create(model=MODEL, max_tokens=500, messages=PREFILLED)


def describe(url):
    return oai.responses.create(
        model="gpt-5",
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": url}]}],
    )


def text_customer(number):
    return sms.messages.create(body="Your order shipped", to=number, from_="+15550000000")


def generic(**params):
    local_client = anthropic.Anthropic()
    return local_client.messages.create(**params)


class Summarizer:
    def __init__(self):
        self.client = OpenAI()

    def run(self, text):
        response = self.client.chat.completions.create(model=self.model, messages=[{"role": "user", "content": text}])
        return response


def two_step(question):
    first = oai.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": question}])
    draft = first.choices[0].message.content
    second = oai.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": draft}])
    raw = second.choices[0].message.content
    return json.loads(raw)
'''

TS_FIXTURE = '''
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import twilio from "twilio";

const MODEL = "gpt-4o-mini";
const openai = new OpenAI();
const anthropic = new Anthropic({ baseURL: process.env.ANTHROPIC_BASE_URL });
const sms = twilio("sid", "token");

export async function classify(text: string) {
  const res = await openai.chat.completions.create({
    model: MODEL,
    messages: [{ role: "user", content: text }],
    response_format: { type: "json_object" },
  });
  return JSON.parse(res.choices[0].message.content!);
}

export const describeImage = async (url: string) =>
  openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: [{ type: "image_url", image_url: { url } }] }] });

class Reviewer {
  async review(diff: string) {
    return anthropic.messages.create({
      model: "claude-sonnet-4-5",
      max_tokens: 1024,
      system: [{ type: "text", text: "Review.", cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: diff }, { role: "assistant", content: "[" }],
    });
  }
}

export async function notify(to: string) {
  return sms.messages.create({ body: "hi", to, from: "+15550000000" });
}

export async function followUp(history: any[], draft: string) {
  const completion = await openai.chat.completions.create({ model: MODEL, messages: [...history, { role: "assistant", content: draft }] });
  const text = completion.choices[0].message.content;
  const second = await openai.chat.completions.create({ model: MODEL, messages: [{ role: "user", content: text! }] });
  return JSON.parse(second.choices[0].message.content!);
}
'''


def by_function(report):
    return {f.function: f for f in report.findings}


def test_supportdesk_call_sites():
    report = scan_repo(SUPPORTDESK)
    sites = by_function(report)
    assert set(sites) == {"triage_ticket", "extract_pii", "draft_reply", "handle_refund_request", "review_case"}

    triage = sites["triage_ticket"]
    assert (triage.sdk, triage.api, triage.model, triage.output) == ("openai", "chat.completions", "gpt-5-mini", "json_object")
    assert (triage.receiver, triage.receiver_kind, triage.module) == ("client", "module_global", "supportdesk.triage")
    assert triage.portability == "auto"

    pii = sites["extract_pii"]
    assert (pii.api, pii.output) == ("chat.completions.parse", "json_schema")
    assert "structured_output" in pii.features

    assert sites["handle_refund_request"].output == "tools"
    assert sites["draft_reply"].portability == "translate"
    assert {"anthropic", "thinking"} <= set(sites["review_case"].features)
    assert sites["review_case"].model == "claude-opus-5-5"
    assert len({f.id for f in report.findings}) == 5


def test_python_edge_cases(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FIXTURE)
    sites = by_function(scan_repo(tmp_path))

    assert "text_customer" not in sites  # Twilio's messages.create is not an LLM call
    assert sites["risks"].model == "claude-sonnet-4-5"  # resolved from the MODEL constant
    assert "prefill" in sites["risks"].features
    assert sites["risks"].portability == "review"

    describe = sites["describe"]
    assert {"responses_api", "vision", "base_url_configurable"} <= set(describe.features)

    generic = sites["generic"]
    assert generic.receiver_kind == "local"
    assert "dynamic_kwargs" in generic.features

    run = sites["Summarizer.run"]
    assert (run.receiver, run.receiver_kind, run.model, run.model_expr) == ("self.client", "attribute", None, "self.model")


def test_json_output_follows_the_data(tmp_path):
    (tmp_path / "app.py").write_text(PYTHON_FIXTURE)
    first, second = [f for f in scan_repo(tmp_path).findings if f.function == "two_step"]
    # Only the second call's result reaches json.loads; the first call's text feeds an LLM call instead.
    assert (first.output, second.output) == ("text", "json_in_text")


def test_typescript_edge_cases(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "llm.ts").write_text(TS_FIXTURE)
    (tmp_path / "src" / "llm.test.ts").write_text(TS_FIXTURE)
    report = scan_repo(tmp_path)
    sites = {f.function: f for f in report.findings if not f.in_tests}

    assert "notify" not in sites
    classify = sites["classify"]
    assert (classify.model, classify.output, classify.receiver_kind) == ("gpt-4o-mini", "json_object", "module_global")
    assert "vision" in sites["describeImage"].features

    review = sites["Reviewer.review"]
    assert (review.sdk, review.line) == ("anthropic", 25)
    assert {"prefill", "prompt_caching", "base_url_configurable"} <= set(review.features)

    first, second = [f for f in report.findings if f.function == "followUp" and not f.in_tests]
    assert "ends_on_assistant" in first.features and first.portability == "review"
    assert (first.output, second.output) == ("text", "json_in_text")

    assert report.summary()["in_tests"] == 5
    assert report.summary()["call_sites"] == 5


HTTP_PY = '''
import json
import os

import httpx
import requests
from fastapi import FastAPI

app = FastAPI()
CHAT_URL = "https://api.openai.com/v1/chat/completions"
BASE = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")


def classify(ticket):
    response = requests.post(CHAT_URL, headers={"Authorization": "Bearer x"}, json={
        "model": "gpt-4o-mini", "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": ticket}]})
    return json.loads(response.json()["choices"][0]["message"]["content"])


async def summarize(text, client: httpx.AsyncClient):
    url = f"{BASE}/chat/completions"
    payload = {"model": MODEL, "messages": [{"role": "user", "content": text}], "stream": True}
    return await client.post(url, json=payload)


def ask_claude(question):
    return httpx.post("https://api.anthropic.com/v1/messages", json={
        "model": "claude-sonnet-5-5", "max_tokens": 512, "messages": [{"role": "user", "content": question}]})


def upload(path):
    return requests.post("https://api.openai.com/v1/files", files={"file": open(path, "rb")})  # not a chat call


def ping():
    return requests.post("https://example.com/hook", json={"ok": True})  # not an LLM endpoint


def notify(chat_id, text):  # Feishu's chat API, not Anthropic's
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    return requests.post(url, params={"receive_id_type": "chat_id"}, json={"receive_id": chat_id, "content": text})


@app.post("/v1/chat/completions")  # serves the endpoint rather than calling it
def serve(body: dict):
    return body
'''

HTTP_TS = """
import axios from "axios";
import ky from "ky";
import express from "express";

const app = express();
const BASE = process.env.LLM_BASE_URL ?? "https://api.openai.com/v1";
const url = `${BASE}/chat/completions`;

export async function triage(ticket: string) {
  const res = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
    body: JSON.stringify({ model: "gpt-4o-mini", tools: [], messages: [{ role: "user", content: ticket }] }),
  });
  return res.json();
}

export async function draft(text: string) {
  const payload = { model: "claude-sonnet-5-5", max_tokens: 256, messages: [{ role: "user", content: text }] };
  return axios.post("https://api.anthropic.com/v1/messages", payload);
}

export async function embed(text: string) {
  return ky.post("https://api.openai.com/v1/embeddings", { json: { model: "text-embedding-3-small", input: text } }).json();
}

export async function probe() {
  return fetch("https://api.openai.com/v1/chat/completions"); // a GET with no body: not a call to the model
}

app.post("/v1/chat/completions", (req, res) => res.json({})); // a route the app serves
router.post("/chat/completions", requireAuth, handleCompletion); // so is this, with imported handlers

export class Messages {  // a generated client for the app's own REST API
  create(options: any) {
    return (options.client ?? this.client).post({ url: "/api/v1/messages", ...options });
  }
}
"""


def test_plain_http_calls_to_llm_endpoints_are_found(tmp_path):
    (tmp_path / "llm.py").write_text(HTTP_PY)
    (tmp_path / "llm.ts").write_text(HTTP_TS)
    sites = {(f.language, f.function): f for f in scan_repo(tmp_path).findings}
    assert set(sites) == {("python", "classify"), ("python", "summarize"), ("python", "ask_claude"),
                          ("typescript", "triage"), ("typescript", "draft"), ("typescript", "embed")}
    classify, summarize, claude = sites["python", "classify"], sites["python", "summarize"], sites["python", "ask_claude"]
    assert (classify.sdk, classify.api, classify.model, classify.output) == ("http", "chat.completions", "gpt-4o-mini", "json_object")
    assert (summarize.model_expr, "streaming" in summarize.features) == ("MODEL", True)  # through f"{BASE}/chat/completions"
    assert (claude.api, claude.model, "anthropic_http" in claude.features) == ("messages", "claude-sonnet-5-5", True)
    triage, draft, embed = sites["typescript", "triage"], sites["typescript", "draft"], sites["typescript", "embed"]
    assert (triage.api, triage.model, triage.output, triage.receiver) == ("chat.completions", "gpt-4o-mini", "tools", "fetch")
    assert (draft.api, draft.model, draft.receiver) == ("messages", "claude-sonnet-5-5", "axios")
    assert (embed.api, embed.output) == ("embeddings", "embedding")
    assert {f.portability for f in sites.values()} == {"review"}


def test_calls_in_docs_samples_benchmarks_and_e2e_arent_the_apps(tmp_path):
    for folder in ("docs/examples", "benchmarks", "e2e"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "call.py").write_text(HTTP_PY)
    report = scan_repo(tmp_path)
    assert report.portable() == [] and report.summary()["in_tests"] == 9  # found, and listed with --include-tests


def test_only_openai_style_http_calls_are_planned_to_move(tmp_path):
    from openport.capture.draft import runnable
    from openport.port.plan import build_plan

    (tmp_path / "llm.py").write_text(HTTP_PY)
    findings = {f.function: f for f in scan_repo(tmp_path).findings}
    plan = build_plan(list(findings.values()), {"nano": "n", "super": "s", "ultra": "u"}, assignments={f.id: "nano" for f in findings.values()})
    assert [plan[findings[name].id].model for name in ("classify", "summarize")] == ["n", "n"]
    claude = findings["ask_claude"]
    assert plan[claude.id].keep and "no Anthropic-style endpoint" in plan[claude.id].reason  # even when a plan names a model
    assert runnable(claude) == (True, "")  # tested through OpenPort's stand-in for the API, though not rewritten


MEDIA_PY = '''
import anthropic
import requests
from openai import OpenAI

client = OpenAI()
claude = anthropic.Anthropic()


def transcribe(audio_b64):  # speech to text, over chat completions
    content = [{"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}]
    payload = {"model": "voxtral-mini", "messages": [{"role": "user", "content": content}]}
    return requests.post("https://api.mistral.ai/v1/chat/completions", json=payload)


def speak(text):
    return client.chat.completions.create(model="gpt-4o-audio-preview", modalities=["text", "audio"], audio={"voice": "alloy", "format": "wav"},
                                          messages=[{"role": "user", "content": text}])


def search(query):
    return requests.post("https://api.perplexity.ai/chat/completions", json={
        "model": "sonar", "messages": [{"role": "user", "content": query}], "web_search_options": {"search_context_size": "low"}})


def research(question):
    return claude.messages.create(model="claude-sonnet-5-5", max_tokens=1024, tools=[{"type": "web_search_20250305", "name": "web_search"}],
                                  messages=[{"role": "user", "content": question}])


def lookup(question):  # the app's own tool, called web_search: not the provider's search
    tools = [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}]
    return client.chat.completions.create(model="gpt-4o", tools=tools, messages=[{"role": "user", "content": question}])
'''

MEDIA_TS = """
export async function listen(audio: string) {
  const body = { model: "gpt-4o-mini-transcribe", messages: [{ role: "user", content: [{ type: "input_audio", input_audio: { data: audio } }] }] };
  return fetch("https://api.openai.com/v1/chat/completions", { method: "POST", body: JSON.stringify(body) });
}

export async function browse(query: string) {
  return fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    body: JSON.stringify({ model: "gpt-4o-search-preview", web_search_options: {}, messages: [{ role: "user", content: query }] }),
  });
}
"""


def test_audio_and_the_providers_own_web_search_keep_a_call_where_it_is(tmp_path):
    from openport.port.plan import build_plan

    (tmp_path / "media.py").write_text(MEDIA_PY)
    (tmp_path / "media.ts").write_text(MEDIA_TS)
    findings = {f.function: f for f in scan_repo(tmp_path).findings}
    expected = {"transcribe": "audio", "speak": "audio", "search": "web_search", "research": "web_search", "listen": "audio", "browse": "web_search"}
    assert {name: feature for name, feature in expected.items() if feature in findings[name].features} == expected
    assert not {"audio", "web_search"} & set(findings["lookup"].features)
    plan = build_plan(list(findings.values()), {"nano": "n", "super": "s", "ultra": "u"})
    assert all(plan[findings[name].id].keep for name in expected) and not plan[findings["lookup"].id].keep
    assert "works in text" in plan[findings["transcribe"].id].reason and "Searches the web" in plan[findings["search"].id].reason


def test_only_files_that_could_hold_a_call_are_parsed(tmp_path):
    """Most of a big repo has no `completions.create`-style pair, so it isn't parsed; a call split over lines still is."""
    (tmp_path / "split.py").write_text(
        "from openai import OpenAI\n\nclient = OpenAI()\n\n\ndef go():\n"
        "    return client.chat.completions \\\n        .create(model='gpt-5', messages=[])\n")
    (tmp_path / "split.ts").write_text(
        "import OpenAI from 'openai';\nconst client = new OpenAI();\n"
        "export async function go() {\n  return client.chat.completions\n    .create({ model: 'gpt-5', messages: [] });\n}\n")
    (tmp_path / "broken.py").write_text("def oops(:\n    pass\n")  # can't parse, and has no call to find
    report = scan_repo(tmp_path)
    assert sorted(f.file for f in report.findings) == ["split.py", "split.ts"]
    assert report.files_scanned == 3 and report.skipped == []
