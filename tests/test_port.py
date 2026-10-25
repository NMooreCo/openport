import os
import subprocess
import sys
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


def test_shared_client_is_split_not_swapped(tmp_path):
    root = write(tmp_path / "repo", SHARED_CLIENT)
    result, sites = port(root, tmp_path / "out")
    app = (result.output / "app.py").read_text()

    assert sites["app.summarize"].status == "ported"
    assert sites["app.describe"].status == "kept" and "vision" in sites["app.describe"].reason
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
    assert runtime.startswith("// @ts-nocheck") and "export class AnthropicOnNemotron" in runtime


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
