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
