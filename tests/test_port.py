import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from openport.port import build_plan, port_repo
from openport.scan import scan_repo

SUPPORTDESK = Path(__file__).parents[1] / "examples" / "supportdesk"
MODELS = {"nano": "nvidia/nano-test", "super": "nvidia/super-test", "ultra": "nvidia/ultra-test"}


def port(root: Path, out: Path, **assignments):
    report = scan_repo(root)
    plan = build_plan(report.findings, MODELS, "super", {k.replace("__", "."): v for k, v in assignments.items()})
    result = port_repo(root, report, plan, out)
    return result, {s.id: s for s in result.sites}


def write(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def test_supportdesk_port(tmp_path):
    result, sites = port(SUPPORTDESK, tmp_path / "out", triage__triage_ticket="nano", policy__review_case="ultra")
    out = result.output

    assert all(s.status == "ported" for s in sites.values())
    assert sites["triage.triage_ticket"].now == "nvidia/nano-test"
    assert sites["policy.review_case"].now == "nvidia/ultra-test"
    assert sorted(result.added) == [".github/workflows/openport.yml", "openport.yaml", "supportdesk/openport_llm.py"]

    triage = (out / "supportdesk/triage.py").read_text()
    assert "from .openport_llm import token_factory_client" in triage
    assert "client = token_factory_client()" in triage
    assert 'model="nvidia/nano-test",  # was "gpt-5-mini"' in triage
    assert "from openai import OpenAI" not in triage

    pii = (out / "supportdesk/pii.py").read_text()
    assert "from openai import OpenAI" not in pii and "from pydantic import BaseModel" in pii

    replies = (out / "supportdesk/replies.py").read_text()
    assert "client = anthropic_client()" in replies and "from anthropic import Anthropic" not in replies

    # The original is untouched.
    assert "client = OpenAI()" in (SUPPORTDESK / "supportdesk/triage.py").read_text()

    # The ported package imports, and its clients point at Token Factory.
    check = (
        "import supportdesk.triage as t, supportdesk.replies as r, supportdesk.refunds as f;"
        "print(type(t.client).__name__, t.client.base_url, type(r.client).__name__, type(f.client).__name__)"
    )
    env = {**os.environ, "NEBIUS_API_KEY": "test-key", "PYTHONPATH": ""}
    ran = subprocess.run([sys.executable, "-c", check], cwd=out, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split() == ["OpenAI", "https://api.tokenfactory.nebius.com/v1/", "AnthropicOnNemotron", "OpenAI"]


def test_supportdesk_tuned_changes_are_written_into_the_code(tmp_path):
    tuned = {
        "triage.triage_ticket": {
            "model": "nano",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "variant": {"system_addendum": "Reply with the JSON object only.", "max_tokens": 800},
        },
        "refunds.handle_refund_request": {
            "model": "super",
            "variant": {"system_addendum": 'Amounts are numbers, as in {"amount": 12.5}.'},
        },
        "replies.draft_reply": {"model": "super", "variant": {"system_addendum": "Never use bullet points.", "max_tokens": 1500}},
        "policy.review_case": {"model": "ultra", "variant": {"system_addendum": "Show amounts with two decimals, e.g. {0:.2f}.", "temperature": 0.3}},
    }
    report = scan_repo(SUPPORTDESK)
    result = port_repo(SUPPORTDESK, report, build_plan(report.findings, MODELS, "super", tuned), tmp_path / "out")
    out = result.output
    assert all(not s.notes for s in result.sites), [s.notes for s in result.sites]

    triage = (out / "supportdesk/triage.py").read_text()
    assert 'Respond with JSON only: {"category": "<category>", "priority": "<priority>"}\n\nReply with the JSON object only."""' in triage
    assert "        response_format={\"type\": \"json_object\"},\n        extra_body={'chat_template_kwargs': {'enable_thinking': False}},\n        max_tokens=800,\n    )" in triage

    replies = (out / "supportdesk/replies.py").read_text()
    assert "max_tokens=1500," in replies and "max_tokens=600" not in replies  # a floor: raised, never lowered
    assert "        temperature=0.3,\n    )" in (out / "supportdesk/policy.py").read_text()

    # The prompts still render: braces in the additions survive .format() and the f-string.
    check = (
        "import supportdesk.refunds as r, supportdesk.policy as p, supportdesk.replies as d;"
        "print(r.SYSTEM_PROMPT.format(today='T', policy='P').splitlines()[-1]);"
        "print(p.SYSTEM_PROMPT.splitlines()[-1]);"
        "print(d.BRAND_VOICE.splitlines()[-1])"
    )
    env = {**os.environ, "NEBIUS_API_KEY": "test-key", "PYTHONPATH": ""}
    ran = subprocess.run([sys.executable, "-c", check], cwd=out, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.splitlines() == [
        'Amounts are numbers, as in {"amount": 12.5}.',
        "Show amounts with two decimals, e.g. {0:.2f}.",
        "Never use bullet points.",
    ]


def test_addendum_lands_in_prompt_files(tmp_path):
    prompt = "You review diffs for security problems.\nReport each finding as JSON with file, line and message.\n"
    root = write(tmp_path / "repo", {
        "prompts/security.md": prompt,
        "requirements.txt": "anthropic\n",  # where the port adds the `openai` package its runtime imports
        "review.py": '''import json
from pathlib import Path

from anthropic import Anthropic

client = Anthropic()


def _load(name):
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text()


def review(diff):
    prompt = _load("security")
    response = client.messages.create(model="claude-opus-4-5", max_tokens=1024, system=prompt, messages=[{"role": "user", "content": diff}])
    return json.loads(response.content[0].text)
''',
    })
    plan = {"review": {"model": "super", "system_prompts": [prompt], "variant": {"system_addendum": "Output only the JSON array."}}}
    result, sites = port(root, tmp_path / "out", **plan)
    assert (result.output / "prompts/security.md").read_text() == prompt.rstrip("\n") + "\n\nOutput only the JSON array.\n"
    assert sites["review"].notes == []


PROBLY_TUNED = {
    "src/constants/messages.ts": "export const SYSTEM_MESSAGE = `You are a spreadsheet assistant.\nAnswer briefly.`;\n",
    "src/pages/api/llm.ts": '''import { OpenAI } from "openai";
import { SYSTEM_MESSAGE } from "@/constants/messages";

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY || "" });

export async function handle(question: string) {
  const messages = [
    { role: "system", content: SYSTEM_MESSAGE },
    { role: "user", content: question },
  ];
  return openai.chat.completions.create({
    messages: messages as any,
    model: "gpt-4o",
  });
}
''',
}


def test_typescript_tuned_changes(tmp_path):
    root = write(tmp_path / "repo", PROBLY_TUNED)
    plan = {"llm.handle": {"model": "super", "variant": {"system_addendum": "Use `A1` notation for cells.", "temperature": 0.2}}}
    result, sites = port(root, tmp_path / "out", **plan)
    assert sites["llm.handle"].notes == []
    constants = (result.output / "src/constants/messages.ts").read_text()
    assert constants == "export const SYSTEM_MESSAGE = `You are a spreadsheet assistant.\nAnswer briefly.\n\nUse \\`A1\\` notation for cells.`;\n"
    llm = (result.output / "src/pages/api/llm.ts").read_text()
    assert '    model: "nvidia/super-test",  // was "gpt-4o"\n    temperature: 0.2,\n  });' in llm


SUPPORTDESK_TS = Path(__file__).parents[1] / "examples" / "supportdesk-ts"


def test_typescript_reasoning_switch_and_settings_go_into_the_call(tmp_path):
    """Tuned like SupportDesk's triage: reasoning off, a token floor and temperature, on a call ending in response_format."""
    variant = {"reasoning": False, "max_tokens": 512, "temperature": 0.0, "system_addendum": "Reply with the JSON object only."}
    plan = {"triage.triageTicket": {"model": "nano", "variant": variant, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                                    "system_prompts": ["You are the triage system for Fernhill Supply Co."]}}
    result, sites = port(SUPPORTDESK_TS, tmp_path / "out", **plan)
    assert sites["triage.triageTicket"].notes == []
    triage = (result.output / "src/triage.ts").read_text()
    assert (
        '    response_format: { type: "json_object" },\n'
        "    // @ts-expect-error Token Factory option (Nemotron's reasoning switch), not in the SDK's types\n"
        "    chat_template_kwargs: { enable_thinking: false },\n"
        "    max_tokens: 512,\n"
        "    temperature: 0,\n"
        "  });"
    ) in triage
    assert triage.count("},,") == 0 and 'Respond with JSON only: {"category": "<category>", "priority": "<priority>"}\n\nReply with the JSON object only.`;' in triage


PREFLIGHT_LIKE = {
    "core/__init__.py": "",
    "core/anthropic_client.py": '''import threading
from anthropic import Anthropic

_client: Anthropic | None = None
_lock = threading.Lock()


def get_client() -> Anthropic:
    global _client
    with _lock:
        if _client is None:
            _client = Anthropic(api_key="sk-ant-old", timeout=20)
    return _client
''',
    "agents/__init__.py": "",
    "agents/review.py": '''import json

import anthropic

from core.anthropic_client import get_client


def review(diff):
    try:
        response = get_client().messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": diff}],
        )
    except anthropic.APIError:
        return None
    return json.loads(response.content[0].text)
''',
}


def test_factory_client_in_another_module(tmp_path):
    root = write(tmp_path / "repo", PREFLIGHT_LIKE)
    result, sites = port(root, tmp_path / "out")
    out = result.output

    client_module = (out / "core/anthropic_client.py").read_text()
    assert "_client = anthropic_client(timeout=20)" in client_module  # the old key is dropped, timeout kept
    assert "from .openport_llm import anthropic_client" in client_module
    assert "from anthropic import Anthropic" in client_module  # still used by the type hints
    assert (out / "core/openport_llm.py").exists()

    agent = (out / "agents/review.py").read_text()
    assert 'model="nvidia/super-test",  # was "claude-opus-4-5"' in agent
    assert "import anthropic" in agent
    assert any("anthropic.APIError" in note for note in sites["review"].notes)


def test_a_factory_shared_with_a_call_that_stays_is_split(tmp_path):
    """preflight, Oct 28: one of four calls on one `get_client()` missed the bar, and the three that passed stayed
    with it. Now they move, on a second factory beside the first; the call that stays keeps the original."""
    root = write(tmp_path / "repo", {**PREFLIGHT_LIKE, "agents/summary.py": '''from core.anthropic_client import get_client


def summarize(text):
    response = get_client().messages.create(model="claude-opus-4-5", max_tokens=300, messages=[{"role": "user", "content": text}])
    return response.content[0].text
'''})
    result, sites = port(root, tmp_path / "out", review="keep")
    out = result.output
    assert (sites["review"].status, sites["summary.summarize"].status) == ("kept", "ported")

    client_module = (out / "core/anthropic_client.py").read_text()
    assert "_client = Anthropic(api_key=\"sk-ant-old\", timeout=20)" in client_module  # the original stays as it was
    assert "@functools.cache\ndef get_client_nemotron():\n" in client_module
    assert "    return anthropic_client(timeout=20)\n" in client_module
    assert "import functools" in client_module and "from .openport_llm import anthropic_client" in client_module

    summary = (out / "agents/summary.py").read_text()
    assert "from core.anthropic_client import get_client\nfrom core.anthropic_client import get_client_nemotron\n" in summary
    assert "response = get_client_nemotron().messages.create(model=\"nvidia/super-test\"" in summary
    review = (out / "agents/review.py").read_text()
    assert review == PREFLIGHT_LIKE["agents/review.py"]  # untouched: it stays on Claude

    # The rewritten module runs: the second factory builds a Token Factory client, once.
    check = (
        "import core.anthropic_client as c; a = c.get_client_nemotron(); "
        "print(type(a).__name__, a is c.get_client_nemotron(), type(c.get_client()).__name__)"
    )
    env = {**os.environ, "NEBIUS_API_KEY": "test-key", "PYTHONPATH": ""}
    ran = subprocess.run([sys.executable, "-c", check], cwd=out, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split() == ["AnthropicOnNemotron", "True", "Anthropic"]


SHARED_CLIENT = {
    "app.py": '''from openai import OpenAI
from vendor_sdk import build

client = OpenAI(timeout=30)
MODEL = "gpt-4o"


def summarize(text):
    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": text}])


def describe(url):
    content = [{"type": "image_url", "image_url": {"url": url}}]
    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": content}])


def mystery(text):
    return build().chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
''',
}


HANDED = {
    "app/__init__.py": "",
    "app/llm.py": '''def ask(client, question):
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": question}])


class Summarizer:
    def __init__(self, llm, model):
        self.llm = llm
        self.model = model

    def run(self, text):
        return self.llm.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": text}])
''',
    "app/main.py": '''from openai import OpenAI

from app.llm import Summarizer, ask

client = OpenAI(timeout=20)
SUMMARIZER = Summarizer(client, "gpt-4o-mini")


def handle(question):
    local = OpenAI()
    return ask(local, question)


def batch(questions):
    return [ask(client, q) for q in questions]
''',
    "tests/test_main.py": '''from unittest.mock import MagicMock

from app.llm import ask


def test_ask():
    ask(MagicMock(), "hi")
''',
}


def test_a_client_handed_in_is_followed_to_where_its_callers_build_it(tmp_path):
    root = write(tmp_path / "repo", HANDED)
    result, sites = port(root, tmp_path / "out")
    main = (result.output / "app/main.py").read_text()
    llm = (result.output / "app/llm.py").read_text()

    assert sites["llm.ask"].status == sites["llm.run"].status == "ported"
    # Both clients its callers build are swapped; the test's MagicMock doesn't count as a caller.
    assert "client = token_factory_client(timeout=20)" in main and "local = token_factory_client()" in main
    assert 'model="nvidia/super-test"' in llm and "token_factory_client" not in llm  # the calls keep their parameter


def test_a_handed_in_client_stays_when_any_caller_passes_another_or_it_is_passed_around(tmp_path):
    groq = {**HANDED, "app/other.py": "from groq import Groq\n\nfrom app.llm import ask\n\n\ndef fast(q):\n    return ask(Groq(), q)\n"}
    result, sites = port(write(tmp_path / "groq", groq), tmp_path / "out1")
    assert sites["llm.ask"].status == "kept" and "whatever its caller passes" in sites["llm.ask"].reason
    # batch() hands ask the client Summarizer is built with: swapping it would move ask's call too, so neither moves.
    assert sites["llm.run"].status == "kept" and "shares its client with llm.ask" in sites["llm.run"].reason
    assert "client = OpenAI(timeout=20)" in (result.output / "app/main.py").read_text()

    callback = {**HANDED, "app/pool.py": "from concurrent.futures import ThreadPoolExecutor\n\nfrom app.llm import ask\n"
                "from app.main import client\n\n\ndef many(qs):\n    with ThreadPoolExecutor() as pool:\n"
                "        return list(pool.map(lambda q: pool.submit(ask, client, q), qs))\n"}
    result, sites = port(write(tmp_path / "callback", callback), tmp_path / "out2")
    assert sites["llm.ask"].status == "kept"  # handed to submit(): a call the port can't see
    assert sites["llm.run"].status == "kept"  # and the callers it can see hand it the client Summarizer uses
    assert "client = OpenAI(timeout=20)" in (result.output / "app/main.py").read_text()

    alone = {**HANDED, "app/main.py": HANDED["app/main.py"].replace("    return [ask(client, q) for q in questions]", "    return []")}
    result, sites = port(write(tmp_path / "alone", {**alone, "app/other.py": groq["app/other.py"]}), tmp_path / "out3")
    assert sites["llm.ask"].status == "kept" and sites["llm.run"].status == "ported"  # no kept call shares Summarizer's client now
    assert "client = token_factory_client(timeout=20)" in (result.output / "app/main.py").read_text()
    assert "local = OpenAI()" in (result.output / "app/main.py").read_text()  # handed to ask, which stays


def test_an_image_call_moves_to_the_nemotron_that_reads_images(tmp_path):
    root = write(tmp_path / "repo", SHARED_CLIENT)
    report = scan_repo(root)
    plan = build_plan(report.findings, {**MODELS, "vision": "nvidia/vl-test"}, "super", {})
    result = port_repo(root, report, plan, tmp_path / "out")
    sites = {s.id: s for s in result.sites}
    app = (result.output / "app.py").read_text()

    assert sites["app.describe"].status == "ported" and sites["app.describe"].now == "nvidia/vl-test"
    assert any("the Nemotron that reads them; check it on your own images" in note for note in sites["app.describe"].notes)
    assert sites["app.summarize"].now == "nvidia/super-test" and not sites["app.summarize"].notes
    # Both calls move, so the client is swapped rather than split, and each call names its own model.
    assert "client = token_factory_client(timeout=30)" in app and "client_nemotron" not in app
    assert 'model="nvidia/vl-test"' in app and 'model="nvidia/super-test"' in app

    tuned = build_plan(report.findings, {**MODELS, "vision": "nvidia/vl-test"}, "super", {"app.describe": {"model": "nvidia/vl-test"}})
    assert tuned["app.describe"].notes == []  # tested by its evals: nothing left to check by hand
    image = next(f for f in report.findings if f.id == "app.describe")
    audio = build_plan([replace(image, features=[*image.features, "audio"])], {**MODELS, "vision": "nvidia/vl-test"})
    assert audio["app.describe"].keep and "audio" in audio["app.describe"].reason  # it reads images, but doesn't hear


def test_shared_client_is_split_not_swapped(tmp_path):
    root = write(tmp_path / "repo", SHARED_CLIENT)
    result, sites = port(root, tmp_path / "out")
    app = (result.output / "app.py").read_text()

    assert sites["app.summarize"].status == "ported"
    assert sites["app.describe"].status == "kept" and "Sends images" in sites["app.describe"].reason
    assert sites["app.mystery"].status == "kept" and "couldn't find where its client" in sites["app.mystery"].reason
    assert sites["app.summarize"].notes == []

    assert "client = OpenAI(timeout=30)\nclient_nemotron = token_factory_client(timeout=30)\n" in app
    assert 'return client_nemotron.chat.completions.create(model="nvidia/super-test"' in app
    # The image call keeps the original client and model constant.
    assert 'MODEL = "gpt-4o"\n' in app
    assert "return client.chat.completions.create(model=MODEL, messages=[{\"role\": \"user\", \"content\": content}])" in app
    assert "from openai import OpenAI" in app and "from openport_llm import token_factory_client" in app


PROBLY_LIKE = {
    "src/pages/api/llm.ts": '''import { OpenAI } from "openai";

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY || "",
});
const MODEL = "gpt-4o";

export async function handle(messages: any[]) {
  const completion = await openai.chat.completions.create({ messages, model: MODEL });
  const tools = await openai.chat.completions.create({ messages, model: MODEL, tools: [] });
  return [completion, tools];
}
''',
    "src/utils/analysisUtils.ts": '''import { OpenAI } from "openai";

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY || "",
});

export async function structure(text: string) {
  return openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });
}

export async function vision(image: string, model: string = "gpt-4o") {
  return openai.chat.completions.create({ model: model, messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: image } }] }] });
}
''',
}


CLAUDE_TS = {
    "src/claude.ts": '''import Anthropic from "@anthropic-ai/sdk";
import { first } from "./helper.js";

const claude = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY, maxRetries: 3 });

export async function label(text: string) {
  const message = await claude.messages.create({
    model: "claude-sonnet-5-5",
    max_tokens: 64,
    system: "Label the message.",
    messages: [{ role: "user", content: text }],
  });
  return first(message);
}
''',
    "src/helper.ts": "export const first = (message: any) => message.content[0].text;\n",
}


def test_typescript_anthropic_port(tmp_path):
    root = write(tmp_path / "repo", CLAUDE_TS)
    result, sites = port(root, tmp_path / "out", claude__label={"model": "super", "variant": {"system_addendum": "One word."}})
    assert sites["claude.label"].status == "ported"
    code = (result.output / "src/claude.ts").read_text()
    assert 'import { AnthropicOnNemotron } from "./openport_llm.js";' in code  # .js, like the file's own imports
    assert "const claude = new AnthropicOnNemotron({ baseURL: process.env.NEBIUS_BASE_URL ??" in code and "maxRetries: 3" in code
    assert "ANTHROPIC_API_KEY" not in code and 'model: "nvidia/super-test",' in code
    assert 'system: "Label the message.\\n\\nOne word."' in code  # the tuned addendum, in Anthropic's own system property
    assert "src/openport_llm.ts" in result.added
    runtime = (result.output / "src/openport_llm.ts").read_text()
    assert runtime.startswith("// @ts-nocheck") and "export const AnthropicOnNemotron: any = AnthropicOnNemotronClient;" in runtime


HTTP_PY = {
    "httpport/__init__.py": "",
    "httpport/llm.py": '''import json
import os

import httpx
import requests

CHAT_URL = "https://api.openai.com/v1/chat/completions"
BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = "gpt-4o-mini"


def classify(ticket):
    response = requests.post(CHAT_URL, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json={
        "model": "gpt-4o-mini", "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": "Label the ticket."}, {"role": "user", "content": ticket}]})
    return json.loads(response.json()["choices"][0]["message"]["content"])


async def summarize(text, client: httpx.AsyncClient):
    key = os.getenv("OPENAI_API_KEY")
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 200
    }
    return await client.post(f"{BASE}/chat/completions", headers=headers, json=payload)


def describe(url):  # sends an image, so it stays, on the URL it shares with classify
    return requests.post(CHAT_URL, headers={"Authorization": "Bearer sk-old"}, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]})


def ask_claude(question):
    return httpx.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"]}, json={
        "model": "claude-sonnet-5-5", "max_tokens": 512, "messages": [{"role": "user", "content": question}]})


class Bot:
    def reply(self, messages):
        return requests.post(f"{self.api_base}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"},
                             json={"model": self.model, "messages": messages})


def wake(host, port, text):  # the app's own server: its host isn't written out, so nothing says it's OpenAI
    return requests.post(f"http://{host}:{port}/v1/chat/completions", json={"model": "agent", "messages": [{"role": "user", "content": text}]})


def local(text):  # a model server on the same machine
    return requests.post("http://localhost:11434/v1/chat/completions", json={"model": "llama3", "messages": [{"role": "user", "content": text}]})
''',
    "openport.evals.yaml": "target: httpport\nsource_root: .\ncall_sites:\n"
                           "  - {id: llm.classify, file: httpport/llm.py, module: httpport.llm, function: classify, intercept: true, sdk: http,\n"
                           "     original_model: gpt-4o-mini, output: json_object, dataset: cases.jsonl, grader: {type: fields, fields: [label]}}\n",
    "cases.jsonl": '{"id": "c1", "input": {"ticket": "I was charged twice"}, "expected": {"label": "billing"}}\n',
}


def test_plain_http_calls_move_to_token_factory(tmp_path, monkeypatch):
    from openport.evals.runner import run_site
    from openport.evals.spec import load_spec

    from .fakes import FakeTokenFactory, completion

    root = write(tmp_path / "repo", HTTP_PY)
    tuned = {"model": "nano", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}, "variant": {"max_tokens": 500}}
    result, sites = port(root, tmp_path / "out", llm__summarize=tuned, llm__classify={"model": "super", "variant": {"system_addendum": "One word."}})
    code = (result.output / "httpport/llm.py").read_text()

    assert sites["llm.classify"].status == sites["llm.summarize"].status == "ported"
    assert sites["llm.describe"].status == "kept" and "image" in sites["llm.describe"].reason
    assert "no Anthropic-style endpoint" in sites["llm.ask_claude"].reason
    assert sites["llm.reply"].status == "kept" and "`f'{self.api_base}/chat/completions'`" in sites["llm.reply"].reason
    assert sites["llm.wake"].status == "kept" and "can't tell where its URL" in sites["llm.wake"].reason
    assert sites["llm.local"].status == "kept" and "same machine" in sites["llm.local"].reason
    assert 'f"http://{host}:{port}/v1/chat/completions"' in code and '"http://localhost:11434/v1/chat/completions"' in code
    # classify: the URL constant still serves the image call, so it gets one of its own; its key and model move too.
    assert ('CHAT_URL = "https://api.openai.com/v1/chat/completions"\n'
            'CHAT_URL_NEMOTRON = "https://api.tokenfactory.nebius.com/v1/chat/completions"\n') in code
    assert "requests.post(CHAT_URL_NEMOTRON, headers={\"Authorization\": f\"Bearer {os.environ['NEBIUS_API_KEY']}\"}" in code
    assert "requests.post(CHAT_URL, headers={\"Authorization\": \"Bearer sk-old\"}" in code
    assert '"model": "nvidia/super-test", "response_format": {"type": "json_object"},  # was "gpt-4o-mini"' in code
    assert '"content": "Label the ticket.\\n\\nOne word."' in code
    # summarize: the base URL and its variable, the key's variable, the model constant, and the tuned settings.
    assert 'BASE = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")' in code
    assert 'key = os.getenv("NEBIUS_API_KEY")' in code and 'MODEL = "nvidia/nano-test"  # was "gpt-4o-mini"' in code
    assert '        "max_tokens": 500,\n        "chat_template_kwargs": {"enable_thinking": False},\n    }' in code
    assert "OPENAI_API_KEY" not in code and 'os.environ["ANTHROPIC_API_KEY"]' in code  # Anthropic's call is untouched
    assert sites["llm.classify"].notes == sites["llm.summarize"].notes  # only the general check for HTTP calls
    assert not any(path.endswith("openport_llm.py") for path in result.added)  # nothing to import for HTTP calls

    # The ported call runs on the model its code names now, through OpenPort's stand-in for the API.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")  # the ported code reads it, as the key OpenPort itself runs with
    fake = FakeTokenFactory(lambda request: completion(content='{"label": "billing"}'))
    spec = load_spec(result.output)
    outcome = run_site(spec, spec.call_sites[0], "as written", None, client=fake)
    assert outcome.pass_rate == 1.0, [case.error for case in outcome.cases]
    assert fake.requests[0]["model"] == "nvidia/super-test"
    for name in ("httpport", "httpport.llm"):
        sys.modules.pop(name, None)


HTTP_TS = {
    "src/llm.ts": '''import axios from "axios";
import ky from "ky";

const BASE = process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1";
const CHAT_URL = "https://api.openai.com/v1/chat/completions";
const MODEL = "gpt-4o-mini";

export async function triage(ticket: string) {
  const res = await fetch(`${BASE}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: "system", content: "Label the ticket." }, { role: "user", content: ticket }]
    }),
  });
  return res.json();
}

export async function draft(text: string) {
  const headers = { Authorization: `Bearer ${import.meta.env.VITE_OPENAI_API_KEY}` };
  const payload = { model: "gpt-4o", messages: [{ role: "user", content: text }], temperature: 0.7 };
  return axios.post(CHAT_URL, payload, { headers });
}

export async function describe(image: string) {  // sends an image, so it stays, on the URL it shares with draft
  const content = [{ type: "image_url", image_url: { url: image } }];
  return ky.post(CHAT_URL, { json: { model: "gpt-4o", messages: [{ role: "user", content }] }, headers: { Authorization: "Bearer sk-old" } }).json();
}

export async function ask(question: string) {
  const body = JSON.stringify({ model: "claude-sonnet-5-5", max_tokens: 256, messages: [{ role: "user", content: question }] });
  return fetch("https://api.anthropic.com/v1/messages", { method: "POST", headers: { "x-api-key": process.env.ANTHROPIC_API_KEY! }, body });
}
''',
}


def test_typescript_plain_http_calls_move_to_token_factory(tmp_path):
    from openport.scan.ts_scanner import _parser

    root = write(tmp_path / "repo", HTTP_TS)
    tuned = {"model": "super", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
             "variant": {"max_tokens": 400, "system_addendum": "One word."}}
    result, sites = port(root, tmp_path / "out", llm__triage=tuned, llm__draft={"model": "nano", "variant": {"temperature": 0.2}})
    code = (result.output / "src/llm.ts").read_text()

    assert [sites[f"llm.{name}"].status for name in ("triage", "draft", "describe", "ask")] == ["ported", "ported", "kept", "kept"]
    # triage: the base URL and its variable, the key's variable, the model constant, and the tuned settings.
    assert 'const BASE = process.env.NEBIUS_BASE_URL ?? "https://api.tokenfactory.nebius.com/v1";' in code
    assert "Authorization: `Bearer ${process.env.NEBIUS_API_KEY}`" in code
    assert 'const MODEL = "nvidia/super-test";  // was "gpt-4o-mini"' in code
    assert ('{ role: "user", content: ticket }],\n'
            "      chat_template_kwargs: { enable_thinking: false },\n      max_tokens: 400,\n    }),") in code
    assert 'content: "Label the ticket.\\n\\nOne word."' in code
    # draft: its URL constant still serves the image call, so it gets one of its own; a Vite variable keeps its prefix.
    assert ('const CHAT_URL = "https://api.openai.com/v1/chat/completions";\n'
            'const CHAT_URL_NEMOTRON = "https://api.tokenfactory.nebius.com/v1/chat/completions";\n') in code
    assert "return axios.post(CHAT_URL_NEMOTRON, payload, { headers });" in code and "ky.post(CHAT_URL, {" in code
    assert "import.meta.env.VITE_NEBIUS_API_KEY" in code
    assert 'const payload = { model: "nvidia/nano-test", messages: [{ role: "user", content: text }], temperature: 0.2 };  // was "gpt-4o"' in code
    assert "OPENAI_API_KEY" not in code and "process.env.ANTHROPIC_API_KEY!" in code  # Anthropic's call is untouched
    assert not _parser(False).parse(code.encode()).root_node.has_error
    assert not any("openport_llm" in path for path in result.added)


TAB_CLIENT_TS = {
    "src/endpoint.ts": (
        'import OpenAI from "openai";\n\nconst DEFAULTS = { timeout: 30_000 };\n\n'
        "export function endpoint(apiKey: string) {\n"
        "\tconst openai = new OpenAI({\n\t\t...DEFAULTS,\n\t\tapiKey,\n\t\tmaxRetries: 2,\n\t});\n"
        '\treturn (text: string) => openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });\n'
        "}\n\n"
        "export function custom(baseURL: string) {  // whoever calls this picks the provider\n"
        '\tconst openai = new OpenAI({ baseURL, apiKey: "sk-" });\n'
        '\treturn (text: string) => openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });\n'
        "}\n"
    ),
    "src/proxy.js": '''const ROUTES = {
  openai: { url: "https://api.openai.com/v1/chat/completions", key: () => process.env.OPENAI_API_KEY },
  gemini: { url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key: () => process.env.GOOGLE_API_KEY },
};

export async function relay(name, body) {
  const route = ROUTES[name];
  return fetch(route.url, { method: "POST", headers: { Authorization: `Bearer ${route.key()}` }, body: JSON.stringify(body) });
}
''',
}


def test_typescript_client_options_the_app_spreads_or_shorthands_dont_win(tmp_path):
    """Like ruflo's: a tab-indented client given its key by shorthand, with options spread in; a client whose caller
    picks the provider; and a table of providers."""
    result, sites = port(write(tmp_path / "repo", TAB_CLIENT_TS), tmp_path / "out")
    code = (result.output / "src/endpoint.ts").read_text()
    assert ("\tconst openai = new OpenAI({\n\t\t...DEFAULTS,\n"
            '\t\tbaseURL: process.env.NEBIUS_BASE_URL ?? "https://api.tokenfactory.nebius.com/v1/",\n'
            '\t\tapiKey: process.env.NEBIUS_API_KEY ?? "",\n\t\tmaxRetries: 2,\n\t});\n') in code
    assert sites["endpoint.custom"].status == "kept" and "from whoever builds it (`baseURL`)" in sites["endpoint.custom"].reason
    assert '\tconst openai = new OpenAI({ baseURL, apiKey: "sk-" });' in code
    assert sites["proxy.relay"].status == "kept"
    assert "several (api.openai.com, generativelanguage.googleapis.com)" in sites["proxy.relay"].reason
    assert (result.output / "src/proxy.js").read_text() == TAB_CLIENT_TS["src/proxy.js"]


FACTORIES = {
    "app/__init__.py": "",
    "app/clients.py": '''import os

from openai import AsyncOpenAI, OpenAI

ROUTER_URL = "https://openrouter.ai/api/v1"


def get_client(*, timeout=30):
    return OpenAI(timeout=timeout)


def get_router_client(prefer_router=False):
    if prefer_router:
        return AsyncOpenAI(api_key="sk-router", base_url=ROUTER_URL)
    return AsyncOpenAI()


def get_proxied_client():
    base_url = os.getenv("LLM_PROXY_URL")  # set by the deployment: still the app's own choice
    return OpenAI(base_url=base_url)


class Gateway:
    def __init__(self, base_url):
        self.client = OpenAI(base_url=base_url)  # whoever builds a Gateway picks the provider

    def ask(self, text):
        return self.client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
''',
    "app/jobs.py": '''from .clients import get_client, get_proxied_client, get_router_client


def proxied(text):
    return get_proxied_client().chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


def summarize(text):
    client = get_client(timeout=10)
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


async def route(text):
    client = get_router_client(prefer_router=True)
    return await client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


def relay(client, text):  # whatever client its caller passes
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
''',
    "web/agents.ts": '''import OpenAI from "openai";

export class Summarizer {
  private client: OpenAI;

  constructor() {
    this.client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY, timeout: 20 });
  }

  async summarize(text: string) {
    return this.client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });
  }
}

export class Tagger {
  client = new OpenAI();

  async tag(text: string) {
    return this.client.chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "user", content: text }] });
  }
}

export async function relay(openai: OpenAI, text: string) {
  return openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });
}
''',
}


def test_clients_from_factories_constructors_and_fields_are_found(tmp_path):
    result, sites = port(write(tmp_path / "repo", FACTORIES), tmp_path / "out")
    assert sites["jobs.summarize"].status == "ported"  # `client = get_client(timeout=10)`, from another module
    assert "def get_client(*, timeout=30):\n    return token_factory_client(timeout=timeout)" in (result.output / "app/clients.py").read_text()
    assert sites["jobs.route"].status == "kept"  # its factory picks a provider: rewriting both branches would hide that
    assert "one of several, for different providers (base URLs ROUTER_URL, the SDK's default)" in sites["jobs.route"].reason
    clients = (result.output / "app/clients.py").read_text()
    assert "return AsyncOpenAI()" in clients
    assert sites["jobs.proxied"].status == "ported" and "    return token_factory_client()\n" in clients  # an environment variable: fixed
    assert sites["clients.ask"].status == "kept" and "from whoever builds it (`base_url=base_url`)" in sites["clients.ask"].reason
    assert "self.client = OpenAI(base_url=base_url)" in clients
    for relay in ("jobs.relay", "agents.relay"):  # a client passed in: the port says which parameter
        assert sites[relay].status == "kept" and "whatever its caller passes (the `" in sites[relay].reason

    agents = (result.output / "web/agents.ts").read_text()
    assert sites["agents.summarize"].status == sites["agents.tag"].status == "ported"
    assert ('this.client = new OpenAI({ baseURL: process.env.NEBIUS_BASE_URL ?? "https://api.tokenfactory.nebius.com/v1/", '
            'apiKey: process.env.NEBIUS_API_KEY ?? "", timeout: 20 });') in agents
    assert 'client = new OpenAI({ baseURL: process.env.NEBIUS_BASE_URL ?? "https://api.tokenfactory.nebius.com/v1/", apiKey:' in agents
    assert "OPENAI_API_KEY" not in agents


SHAPES = {
    "app/__init__.py": "",
    "app/shapes.py": '''from groq import Groq
from library import LibraryClient
from openai import OpenAI
from tracing import instrument

traced = instrument(OpenAI(timeout=10))  # wrapped by a library: the client inside is what moves


def summarize(text):
    return traced.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


def quick(text):
    client = OpenAI()
    return client.with_options(timeout=5).chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": text}])


def fast(text):
    client = Groq(api_key="gsk-old")
    return client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": text}])


class Agent:
    def __init__(self, client):
        self.client = client

    def run(self, text):
        return self.client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


class _Base:
    def __init__(self):
        self._client = OpenAI(max_retries=2)


class Chat(_Base):  # its client comes from the class it extends, here in this file
    def ask(self, text):
        def attempt():  # and the call is in a function inside the method
            return self._client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
        return attempt()


class Graph(LibraryClient):  # set up by a class from a library
    def ask(self, text):
        return self.client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
''',
    "web/shapes.ts": '''import OpenAI from "openai";
import { withTracing } from "tracing";
import { GroqClient } from "groq-client";

const traced = withTracing(new OpenAI());

export async function summarize(text: string) {
  return traced.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] });
}

export class Agent {
  client: OpenAI;
  constructor(client: OpenAI) { this.client = client; }
  async run(text: string) { return this.client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: text }] }); }
}

export class Fast {
  client = new GroqClient();
  async go(text: string) { return this.client.chat.completions.create({ model: "llama-3", messages: [{ role: "user", content: text }] }); }
}
''',
}


def test_wrapped_and_copied_clients_move_and_other_clients_say_why_they_stay(tmp_path):
    result, sites = port(write(tmp_path / "repo", SHAPES), tmp_path / "out")
    code = (result.output / "app/shapes.py").read_text()
    assert sites["shapes.summarize"].status == "ported" and "traced = instrument(token_factory_client(timeout=10))" in code
    assert sites["shapes.quick"].status == "ported" and "    client = token_factory_client()\n" in code  # through .with_options(...)
    assert "is a `Groq`, not one of OpenAI's or Anthropic's SDK clients" in sites["shapes.fast"].reason
    assert "the `client` parameter of `__init__`" in sites["shapes.run"].reason
    assert sites["shapes.attempt"].status == "ported" and "self._client = token_factory_client(max_retries=2)" in code
    assert "set up by the class it extends (`LibraryClient`)" in sites["shapes.ask"].reason
    web = (result.output / "web/shapes.ts").read_text()
    assert sites["shapes.summarize-2"].status == "ported" and "withTracing(new OpenAI({ baseURL: process.env.NEBIUS_BASE_URL" in web
    assert "the `client` parameter of `constructor`" in sites["shapes.run-2"].reason
    assert "is a `GroqClient`" in sites["shapes.go"].reason


def test_typescript_port(tmp_path):
    root = write(tmp_path / "repo", PROBLY_LIKE)
    result, sites = port(root, tmp_path / "out")
    out = result.output

    llm = (out / "src/pages/api/llm.ts").read_text()
    assert 'baseURL: process.env.NEBIUS_BASE_URL ?? "https://api.tokenfactory.nebius.com/v1/",' in llm  # overridable, as in Python
    assert 'apiKey: process.env.NEBIUS_API_KEY ?? "",' in llm
    assert "OPENAI_API_KEY" not in llm
    assert 'const MODEL = "nvidia/super-test";  // was "gpt-4o"' in llm  # shared constant, same target: edited once

    utils = (out / "src/utils/analysisUtils.ts").read_text()
    assert "const openaiNemotron = new OpenAI({" in utils
    assert 'return openaiNemotron.chat.completions.create({ model: "nvidia/super-test"' in utils
    assert "return openai.chat.completions.create({ model: model," in utils  # the image call is untouched
    assert sites["analysisUtils.vision"].status == "kept"
    assert sites["analysisUtils.vision"].was == "set by `model`"
    assert not any(path.endswith("openport_llm.py") for path in result.added)


def _can_link(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "probe-link")
        return True
    except OSError:  # Windows without the privilege to make links
        return False


def test_a_link_out_of_the_repo_is_never_read_into_the_diff_or_written_through(tmp_path):
    import pytest

    if not _can_link(tmp_path):
        pytest.skip("this account can't create symlinks")
    secret = tmp_path / "server.env"
    secret.write_text("NEBIUS_API_KEY=the-real-key\n")
    repo = write(tmp_path / "repo", {"app.py": 'from openai import OpenAI\nclient = OpenAI()\n\n\ndef ask(q):\n'
                                               '    return client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": q}])\n'})
    os.symlink(secret, repo / "openport.yaml")  # a repo can commit a link to any path on the machine that ports it
    result, _ = port(repo, tmp_path / "out")
    assert "the-real-key" not in result.diff  # the diff is shown on the dashboard and saved
    written = result.output / "openport.yaml"
    assert not written.is_symlink() and "the-real-key" not in written.read_text()
    assert secret.read_text() == "NEBIUS_API_KEY=the-real-key\n"  # nothing written through the link


def test_a_link_out_of_the_repo_stays_a_link_in_an_upgrade_copy(tmp_path):
    import pytest

    from openport.upgrade.apply import apply_bumps, copy_repo

    if not _can_link(tmp_path):
        pytest.skip("this account can't create symlinks")
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n")
    repo = write(tmp_path / "repo", {"requirements.txt": "requests==2.31.0\n"})
    os.symlink(secret, repo / "key")
    os.symlink(secret, repo / "requirements-dev.txt")
    copy = copy_repo(repo, tmp_path / "copy")
    assert (copy / "key").is_symlink()  # the tests' container sees a link into itself, not the host's file
    assert apply_bumps(copy, {"requests": "2.33.0"}) == ["requirements.txt"]  # the linked requirements file is left alone
    assert secret.read_text() == "PRIVATE KEY\n"
