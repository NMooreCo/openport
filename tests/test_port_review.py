"""Cases from a review of the port: each one a way a ported app broke, or a port went somewhere it shouldn't."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openport import config
from openport.port import build_plan, port_repo
from openport.runtime.openport_llm import AnthropicOnNemotron, openai_to_anthropic
from openport.scan import scan_repo
from openport.scan.findings import CallSiteFinding, features_for_api, is_test_path

from .fakes import FakeTokenFactory, completion
from .test_port import MODELS, port, write

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
TSC = Path(__file__).parents[1] / "examples" / "supportdesk-ts" / "node_modules" / "typescript" / "bin" / "tsc"
needs_tsc = pytest.mark.skipif(shutil.which("node") is None or not TSC.is_file(),
                               reason="needs Node.js and `npm install` in examples/supportdesk-ts")
RUNTIME_JS = Path(__file__).parents[1] / "src" / "openport" / "runtime" / "openport_llm.mjs"


def run_python(cwd: Path, code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "NEBIUS_API_KEY": "test-key", "PYTHONPATH": ""}
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True)


def run_node(tmp_path: Path, code: str):
    script = tmp_path / "check.mjs"
    script.write_text(f"import * as runtime from {json.dumps(RUNTIME_JS.as_uri())};\n{code}\n")
    return json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)


# --- 1. `.beta` and `.with_options(...)` on Anthropic's stand-ins ------------------------------------------------


BETA_APP = {
    "app.py": '''from anthropic import Anthropic

client = Anthropic()


def ask(q):
    return client.beta.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}], betas=["x"])


def quick(q):
    return client.with_options(timeout=5).messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}])
''',
}


def test_anthropic_calls_through_beta_and_with_options_run_on_the_stand_in(tmp_path, monkeypatch):
    fake = FakeTokenFactory(lambda request: completion(content="ok"))
    client = AnthropicOnNemotron(client=fake)
    reply = client.beta.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "hi"}], betas=["b"])
    assert reply.content[0].text == "ok" and "betas" not in fake.requests[0]

    from openport.runtime import openport_llm

    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    for build, kind in ((openport_llm.anthropic_client, openport_llm.AnthropicOnNemotron),
                        (openport_llm.async_anthropic_client, openport_llm.AsyncAnthropicOnNemotron)):
        original = build(timeout=20)
        copy = original.with_options(timeout=5, max_retries=0, api_key="sk-ant-old")  # Anthropic's key never reaches Nebius
        assert type(copy) is kind and copy is not original and type(copy.messages) is type(original.messages)
        assert (copy._client.timeout, copy._client.max_retries, copy._client.api_key) == (5, 0, "test-key")
        assert original._client.timeout == 20 and copy.beta is copy

    result, sites = port(write(tmp_path / "repo", BETA_APP), tmp_path / "out")
    assert sites["app.ask"].status == sites["app.quick"].status == "ported"  # `client.beta` is the client
    ran = run_python(result.output, "import app; print(app.client.beta.messages.create is not None, "
                                    "type(app.client.with_options(timeout=5)).__name__)")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split() == ["True", "AnthropicOnNemotron"]


@needs_node
def test_the_javascript_stand_in_has_beta_and_with_options(tmp_path):
    checks = run_node(tmp_path, """const client = new runtime.AnthropicOnNemotron({ apiKey: "nb", baseURL: "http://tf/v1", timeout: 20 });
const copy = client.withOptions({ timeout: 5, apiKey: "sk-ant-old", baseURL: "https://api.anthropic.com" });
process.stdout.write(JSON.stringify([client.beta === client, copy !== client, copy.timeout, copy.apiKey, copy.baseURL,
  typeof copy.beta.messages.create, client.timeout]));""")
    assert checks == [True, True, 5, "nb", "http://tf/v1", "function", 20]


# --- 2 and 10. Ported TypeScript type-checks, and imports the runtime the way the file imports its own modules ----


TS_PROJECT = {
    "package.json": '{"name": "claude-app", "type": "module"}',
    "tsconfig.json": json.dumps({"compilerOptions": {
        "target": "es2023", "module": "nodenext", "moduleResolution": "nodenext", "strict": True, "noEmit": True,
        "allowImportingTsExtensions": True, "skipLibCheck": True, "types": []}, "include": ["src"]}),
    "node_modules/@anthropic-ai/sdk/package.json": json.dumps({
        "name": "@anthropic-ai/sdk", "type": "module", "exports": {".": {"types": "./index.d.ts", "default": "./index.mjs"}}}),
    "node_modules/@anthropic-ai/sdk/index.d.ts": """type Params = { model: string; max_tokens: number; system?: string; messages: { role: "user" | "assistant"; content: string }[] };
type Messages = { create(params: Params): Promise<{ content: { type: "text"; text: string }[] }> };
export default class Anthropic {
  constructor(options?: { apiKey?: string; maxRetries?: number });
  messages: Messages;
  beta: { messages: Messages };
}
""",
    "src/env.d.ts": "declare const process: { env: Record<string, string | undefined> };\n",
    "src/helper.ts": "export const first = (message: { content: { text: string }[] }): string => message.content[0].text;\n",
    "src/claude.ts": '''import Anthropic from "@anthropic-ai/sdk";
import { first } from "./helper.ts";

const claude = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY, maxRetries: 3 });

export async function label(text: string): Promise<string> {
  const message = await claude.messages.create({
    model: "claude-sonnet-5-5",
    max_tokens: 64,
    system: "Label the message.",
    messages: [{ role: "user", content: text }],
  });
  return first(message);
}

export async function tag(text: string): Promise<string> {
  const message = await claude.beta.messages.create({ model: "claude-haiku-5", max_tokens: 16, messages: [{ role: "user", content: text }] });
  return message.content[0].text;
}
''',
}


def tsc(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(TSC), "-p", str(project)], capture_output=True, text=True)


@needs_tsc
def test_ported_typescript_anthropic_code_type_checks(tmp_path):
    root = write(tmp_path / "repo", TS_PROJECT)
    assert tsc(root).returncode == 0, tsc(root).stdout  # it did before the port
    tuned = {"model": "nano", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}, "variant": {"max_tokens": 128}}
    result, sites = port(root, tmp_path / "out", claude__label=tuned)
    assert sites["claude.label"].status == sites["claude.tag"].status == "ported"
    shutil.copytree(root / "node_modules", result.output / "node_modules")
    checked = tsc(result.output)
    assert checked.returncode == 0, checked.stdout


def test_the_runtime_import_mirrors_the_files_own_ts_extensions(tmp_path):
    result, _ = port(write(tmp_path / "repo", TS_PROJECT), tmp_path / "out")
    assert 'import { AnthropicOnNemotron } from "./openport_llm.ts";' in (result.output / "src/claude.ts").read_text()


def test_the_anthropic_stand_in_takes_token_factory_options_in_extra_body(tmp_path):
    """The stand-in's types are unchecked, so no @ts-expect-error (one with nothing to expect is an error itself)."""
    tuned = {"model": "nano", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    result, sites = port(write(tmp_path / "repo", TS_PROJECT), tmp_path / "out", claude__label=tuned)
    code = (result.output / "src/claude.ts").read_text()
    assert sites["claude.label"].notes == [] and "@ts-expect-error" not in code
    assert '    messages: [{ role: "user", content: text }],\n    extra_body: { chat_template_kwargs: { enable_thinking: false } },\n  });' in code


# --- 3. A client that also serves calls the scanner can't see ------------------------------------------------------


OTHER_USES = {
    "app/__init__.py": "",
    "app/llm.py": '''from openai import OpenAI

client = OpenAI(timeout=30)


def summarize(text):
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])


def transcribe(audio):
    return client.audio.transcriptions.create(model="whisper-1", file=audio).text


def flag(text):
    local = OpenAI()
    if local.moderations.create(input=text).results[0].flagged:
        return None
    return local.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": text}])
''',
    "app/claude.py": '''from anthropic import Anthropic

claude = Anthropic()


def answer(question):
    return claude.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=[{"role": "user", "content": question}])
''',
    "app/budget.py": '''from app.claude import claude


def cost(question):
    return claude.messages.count_tokens(model="claude-sonnet-5-5", messages=[{"role": "user", "content": question}])
''',
    "web/llm.ts": '''import OpenAI from "openai";

const openai = new OpenAI();

export async function ask(q: string) {
  return openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}

export async function models() {
  return openai.models.list();
}
''',
    "web/shadow.ts": '''import OpenAI from "openai";

const openai = new OpenAI();

export async function answer(q: string) {
  return openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}

export async function modelsFor(apiKey: string) {
  const openai = new OpenAI({ apiKey });  // its own client, not the one above
  return openai.models.list();
}
''',
}


def test_a_client_that_also_makes_calls_the_port_cant_move_keeps_its_original(tmp_path):
    result, sites = port(write(tmp_path / "repo", OTHER_USES), tmp_path / "out")
    llm = (result.output / "app/llm.py").read_text()
    # transcription stays on OpenAI: the chat call gets a client of its own.
    assert sites["llm.summarize"].status == "ported"
    assert "client = OpenAI(timeout=30)\nclient_nemotron = token_factory_client(timeout=30)\n" in llm
    assert "return client_nemotron.chat.completions.create(" in llm and "return client.audio.transcriptions.create(" in llm
    # A local client can't be split: the call stays, and says why.
    assert sites["llm.flag"].status == "kept" and "`local.moderations.create`" in sites["llm.flag"].reason
    assert "    local = OpenAI()\n" in llm
    # Another module counts tokens with Claude's client: it keeps it.
    claude = (result.output / "app/claude.py").read_text()
    assert sites["claude.answer"].status == "ported" and "claude = Anthropic()\nclaude_nemotron = anthropic_client()\n" in claude
    assert "return claude_nemotron.messages.create(" in claude
    assert (result.output / "app/budget.py").read_text() == OTHER_USES["app/budget.py"]

    web = (result.output / "web/llm.ts").read_text()
    assert sites["llm.ask"].status == "ported" and "const openai = new OpenAI();\nconst openaiNemotron = new OpenAI({" in web
    assert "return openaiNemotron.chat.completions.create(" in web and "return openai.models.list();" in web
    shadow = (result.output / "web/shadow.ts").read_text()
    assert sites["shadow.answer"].status == "ported" and "openaiNemotron" not in shadow  # swapped: the other client is another
    assert "const openai = new OpenAI({ baseURL: process.env.NEBIUS_BASE_URL" in shadow and "new OpenAI({ apiKey });" in shadow


# --- 4. An app that only used Anthropic's SDK gets the `openai` package the runtime imports -----------------------


CLAUDE_ONLY = '''from anthropic import Anthropic

client = Anthropic()


def ask(q):
    return client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}])
'''


def test_the_runtimes_openai_dependency_is_added(tmp_path):
    requirements = {"app.py": CLAUDE_ONLY, "requirements.txt": "anthropic>=0.40  # Claude\nfastapi\n"}
    result, sites = port(write(tmp_path / "req", requirements), tmp_path / "out1")
    assert (result.output / "requirements.txt").read_text() == "anthropic>=0.40  # Claude\nfastapi\nopenai>=1.0\n"
    assert "requirements.txt" in result.changed and sites["app.ask"].notes == []

    pyproject = {"app.py": CLAUDE_ONLY, "pyproject.toml": '[project]\nname = "x"\ndependencies = [\n    "anthropic>=0.40",\n    "fastapi",\n]\n\n'
                                                         '[project.optional-dependencies]\ndev = ["pytest"]\n'}
    result, _ = port(write(tmp_path / "pyproject", pyproject), tmp_path / "out2")
    assert (result.output / "pyproject.toml").read_text() == ('[project]\nname = "x"\ndependencies = [\n    "openai>=1.0",\n    "anthropic>=0.40",\n'
                                                              '    "fastapi",\n]\n\n[project.optional-dependencies]\ndev = ["pytest"]\n')

    already = {"app.py": CLAUDE_ONLY, "requirements.txt": "anthropic\nopenai==2.1\n"}
    result, _ = port(write(tmp_path / "already", already), tmp_path / "out3")
    assert "requirements.txt" not in result.changed

    poetry = {"app.py": CLAUDE_ONLY, "pyproject.toml": '[tool.poetry.dependencies]\nanthropic = "^0.40"\n'}
    result, sites = port(write(tmp_path / "poetry", poetry), tmp_path / "out4")
    assert "pyproject.toml" not in result.changed
    assert any("`openai` package" in note for note in sites["app.ask"].notes)


# --- 5. Tuned settings never collide with a `**kwargs` the call passes ---------------------------------------------


def test_tuned_settings_are_not_inserted_beside_a_spread(tmp_path):
    app = '''from openai import OpenAI

client = OpenAI()
DEFAULTS = {"max_tokens": 300, "temperature": 0.5}


def ask(q):
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": q}], **DEFAULTS)


def tuned(q):
    return client.chat.completions.create(model="gpt-4o", temperature=0.9, messages=[{"role": "user", "content": q}], **DEFAULTS)
'''
    variant = {"max_tokens": 800, "temperature": 0.2}
    tuned = {"model": "nano", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}, "variant": variant}
    result, sites = port(write(tmp_path / "repo", {"app.py": app}), tmp_path / "out", app__ask=tuned, app__tuned=tuned)
    code = (result.output / "app.py").read_text()
    assert '"content": q}], **DEFAULTS)  # was "gpt-4o"' in code and "max_tokens=800" not in code and "extra_body" not in code
    assert "temperature=0.2, messages" in code  # written out at the call: it can be set
    notes = " ".join(sites["app.ask"].notes)
    assert "`**DEFAULTS`" in notes and "800" in notes and "0.2" in notes and "enable_thinking" in notes


# --- 6. A model constant or prompt that a call that stays uses too -------------------------------------------------


SHARED_VALUES = {
    "app/__init__.py": "",
    "app/llm.py": '''from openai import OpenAI

client = OpenAI()
MODEL = "gpt-4o"
SYSTEM = "You are terse."


def ask(question):
    return client.chat.completions.create(model=MODEL, messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}])
''',
    "app/vision.py": '''from openai import OpenAI

from app.llm import MODEL, SYSTEM

client = OpenAI()


def look(url):
    content = [{"type": "image_url", "image_url": {"url": url}}]
    return client.chat.completions.create(model=MODEL, messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}])
''',
    "app/count.py": '''import tiktoken
from openai import OpenAI

client = OpenAI()
MODEL = "gpt-4o-mini"
ENCODING = tiktoken.encoding_for_model(MODEL)


def short(text):
    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": text}])
''',
    "web/llm.ts": '''import OpenAI from "openai";

const openai = new OpenAI();
export const MODEL = "gpt-4o";

export async function ask(q: string) {
  return openai.chat.completions.create({ model: MODEL, messages: [{ role: "user", content: q }] });
}
''',
    "web/vision.ts": '''import OpenAI from "openai";
import { MODEL } from "./llm";

const openai = new OpenAI();

export async function look(url: string) {
  return openai.chat.completions.create({ model: MODEL, messages: [{ role: "user", content: [{ type: "image_url", image_url: { url } }] }] });
}
''',
}


def test_a_constant_another_module_or_call_still_reads_is_left_for_them(tmp_path):
    result, sites = port(write(tmp_path / "repo", SHARED_VALUES), tmp_path / "out",
                         llm__ask={"model": "super", "variant": {"system_addendum": "One sentence."}})
    out = result.output
    assert sites["vision.look"].status == sites["vision.look-2"].status == "kept"
    llm = (out / "app/llm.py").read_text()
    assert 'MODEL = "gpt-4o"\nSYSTEM = "You are terse."\n' in llm  # still the kept image call's model and prompt
    assert 'create(model="nvidia/super-test", messages=[{"role": "system", "content": SYSTEM}' in llm
    assert any("One sentence." in note and "a call that stays" in note for note in sites["llm.ask"].notes)
    count = (out / "app/count.py").read_text()
    assert 'MODEL = "gpt-4o-mini"\n' in count and 'create(model="nvidia/super-test"' in count  # tiktoken still reads it
    web = (out / "web/llm.ts").read_text()
    assert 'export const MODEL = "gpt-4o";' in web and '{ model: "nvidia/super-test", messages' in web


def test_a_constant_only_ported_calls_read_is_still_edited_once(tmp_path):
    files = {k: v for k, v in SHARED_VALUES.items() if k in ("app/__init__.py", "app/llm.py")}
    result, sites = port(write(tmp_path / "repo", files), tmp_path / "out", llm__ask={"model": "super", "variant": {"system_addendum": "One sentence."}})
    llm = (result.output / "app/llm.py").read_text()
    assert 'MODEL = "nvidia/super-test"  # was "gpt-4o"\nSYSTEM = "You are terse.\\n\\nOne sentence."\n' in llm
    assert sites["llm.ask"].notes == []


# --- 7. The output folder: never the source, never inside it, never around it ---------------------------------------


APP = {"app.py": 'from openai import OpenAI\n\nclient = OpenAI()\n\n\ndef ask(q):\n'
                 '    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": q}])\n'}


def test_the_output_folder_never_overlaps_the_source(tmp_path, monkeypatch):
    repo = write(tmp_path / "repo", APP)
    report = scan_repo(repo)
    plan = build_plan(report.findings, MODELS)
    with pytest.raises(FileExistsError, match="is the repo itself"):
        port_repo(repo, report, plan, repo)
    assert sorted(p.name for p in repo.iterdir()) == ["app.py"] and (repo / "app.py").read_text() == APP["app.py"]

    # `openport port .`: the copy goes to runs/ports/<name> inside the repo, and leaves runs/ out of itself, run after run
    monkeypatch.setattr(config, "RUNS_DIR", repo / "runs")
    for _ in range(2):
        result = port_repo(repo, scan_repo(repo), plan, repo / "runs" / "ports" / "repo")
        assert sorted(p.name for p in result.output.iterdir()) == ["app.py", "openport_llm.py", "openport.yaml"] or not (result.output / "runs").exists()
        assert not (result.output / "runs").exists()
    assert (repo / "app.py").read_text() == APP["app.py"]

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")  # a folder OpenPort manages, and would empty first
    inner = write(tmp_path / "runs" / "ports" / "app" / "repo", APP)
    with pytest.raises(FileExistsError, match="would delete it"):
        port_repo(inner, scan_repo(inner), plan, tmp_path / "runs" / "ports" / "app")
    assert (inner / "app.py").read_text() == APP["app.py"]


# --- 8. A Python file that starts with a UTF-8 byte order mark ----------------------------------------------------


def test_a_file_with_a_byte_order_mark_is_scanned_and_ported_byte_for_byte(tmp_path):
    source = ('﻿MODEL = "gpt-4o"\nfrom openai import OpenAI\n\nclient = OpenAI()\n\n\ndef ask(q):\n'
              '    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": q}])\n')
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "app.py").write_bytes(source.encode("utf-8"))
    result, sites = port(tmp_path / "repo", tmp_path / "out")
    assert sites["app.ask"].status == "ported"
    assert (result.output / "app.py").read_bytes().decode("utf-8") == (
        '﻿MODEL = "nvidia/super-test"  # was "gpt-4o"\nfrom openport_llm import token_factory_client\n\n'
        "client = token_factory_client()\n\n\ndef ask(q):\n"
        '    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": q}])\n')


# --- 9 and 11. The runtime's import comes before the client it builds, and the runtime sits where it's importable --


LATE_IMPORT = '''"""Answers questions."""
from anthropic import Anthropic

client = Anthropic(timeout=10)

import json  # after the client, as some scripts do


def ask(question):
    response = client.messages.create(model="claude-sonnet-5-5", max_tokens=100, messages=[{"role": "user", "content": question}])
    return json.dumps(response.content[0].text)
'''


def test_the_runtime_import_comes_before_the_client_and_beside_a_script(tmp_path):
    result, sites = port(write(tmp_path / "repo", {"backend/main.py": LATE_IMPORT}), tmp_path / "out")
    assert sites["main.ask"].status == "ported"
    assert "backend/openport_llm.py" in result.added and "openport_llm.py" not in result.added  # beside the script, not at the root
    main = (result.output / "backend/main.py").read_text()
    assert main.startswith('"""Answers questions."""\nif __package__:\n    from .openport_llm import anthropic_client\nelse:\n'
                           '    from openport_llm import anthropic_client\n\nclient = anthropic_client(timeout=10)\n')
    ran = run_python(result.output / "backend", "import main; print(type(main.client).__name__)")  # as `python main.py` runs it
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "AnthropicOnNemotron"


def test_a_second_factorys_functools_import_comes_before_it(tmp_path):
    files = {
        "core/__init__.py": "",
        "core/llm.py": '''from anthropic import Anthropic


def get_client():
    return Anthropic(timeout=20)


from .settings import DEBUG  # noqa: E402
''',
        "core/settings.py": "DEBUG = False\n",
        "core/jobs.py": '''from core.llm import get_client


def review(diff):
    return get_client().messages.create(model="claude-opus-4-5", max_tokens=100, messages=[{"role": "user", "content": diff}])


def summary(text):
    return get_client().messages.create(model="claude-opus-4-5", max_tokens=100, messages=[{"role": "user", "content": text}])
''',
    }
    result, sites = port(write(tmp_path / "repo", files), tmp_path / "out", jobs__review="keep")
    assert sites["jobs.summary"].status == "ported"
    ran = run_python(result.output, "import core.llm as m; print(type(m.get_client_nemotron()).__name__)")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "AnthropicOnNemotron"


# --- 12. stop_reason when the reply calls tools --------------------------------------------------------------------


def test_a_reply_with_tool_calls_stops_for_tool_use_unless_it_ran_out_of_tokens():
    calls = [("call_1", "lookup", {"id": "7"})]
    assert openai_to_anthropic(completion(tool_calls=calls, finish_reason="stop"), model="m", include_thinking=False).stop_reason == "tool_use"
    assert openai_to_anthropic(completion(tool_calls=calls, finish_reason="length"), model="m", include_thinking=False).stop_reason == "max_tokens"
    assert openai_to_anthropic(completion(content="hi"), model="m", include_thinking=False).stop_reason == "end_turn"


@needs_node
def test_the_javascript_runtime_stops_for_tool_use_too(tmp_path):
    reasons = run_node(tmp_path, """const call = { id: "c1", type: "function", function: { name: "lookup", arguments: "{}" } };
const reply = (finish_reason) => ({ id: "r", choices: [{ finish_reason, message: { content: null, tool_calls: [call] } }] });
process.stdout.write(JSON.stringify(["stop", "length", "tool_calls"].map((f) => runtime.openAIToAnthropic(reply(f), "m", false).stop_reason)));""")
    assert reasons == ["tool_use", "max_tokens", "tool_use"]


# --- 13. No `# was` comment where the line ends inside a string -----------------------------------------------------


def test_no_was_comment_lands_inside_a_string(tmp_path):
    files = {
        "app.py": '''from anthropic import Anthropic

client = Anthropic()


def label(text):
    response = client.messages.create(model="claude-sonnet-5-5", max_tokens=50, system="""Label the text.
Use one word.""", messages=[{"role": "user", "content": text}])
    return response.content[0].text
''',
        "web/label.ts": '''import OpenAI from "openai";

const openai = new OpenAI();

export async function label(text: string) {
  return openai.chat.completions.create({ model: "gpt-4o", messages: [{ role: "system", content: `Label the text.
Use one word.` }, { role: "user", content: text }] });
}
''',
    }
    result, _ = port(write(tmp_path / "repo", files), tmp_path / "out")
    code = (result.output / "app.py").read_text()
    assert 'system="""Label the text.\nUse one word."""' in code and "# was" not in code
    web = (result.output / "web/label.ts").read_text()
    assert "content: `Label the text.\nUse one word.` }" in web and "// was" not in web


# --- 14. A TypeScript client split keeps the constructor the file imports ------------------------------------------


def test_a_typescript_split_builds_the_client_the_way_the_file_does(tmp_path):
    files = {"src/llm.ts": '''import * as oai from "openai";

const client = new oai.OpenAI({ timeout: 10 });

export async function ask(q: string) {
  return client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}

export async function look(url: string) {
  return client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: [{ type: "image_url", image_url: { url } }] }] });
}
'''}
    result, sites = port(write(tmp_path / "repo", files), tmp_path / "out")
    code = (result.output / "src/llm.ts").read_text()
    assert sites["llm.ask"].status == "ported" and sites["llm.look"].status == "kept"
    assert "const clientNemotron = new oai.OpenAI({ " in code and "new OpenAI(" not in code


# --- 15. A factory split that would read the factory's own locals --------------------------------------------------


def test_a_factory_whose_client_reads_its_parameters_isnt_split(tmp_path):
    files = {
        "core/__init__.py": "",
        "core/llm.py": "from anthropic import Anthropic\n\n\ndef get_client(timeout=20):\n    return Anthropic(timeout=timeout)\n",
        "core/jobs.py": '''from core.llm import get_client


def review(diff):
    return get_client().messages.create(model="claude-opus-4-5", max_tokens=100, messages=[{"role": "user", "content": diff}])


def summary(text):
    return get_client().messages.create(model="claude-opus-4-5", max_tokens=100, messages=[{"role": "user", "content": text}])
''',
    }
    result, sites = port(write(tmp_path / "repo", files), tmp_path / "out", jobs__review="keep")
    assert sites["jobs.summary"].status == "kept" and "`timeout`" in sites["jobs.summary"].reason
    assert (result.output / "core/llm.py").read_text() == files["core/llm.py"]


# --- 16. An assignment can't move a call the runtime can't run -----------------------------------------------------


def test_an_assignment_doesnt_override_what_the_runtime_cant_do():
    def finding(api: str, sdk: str = "anthropic") -> CallSiteFinding:
        return CallSiteFinding(file="app.py", line=1, language="python", function="f", sdk=sdk, api=api, receiver="client",
                               receiver_kind="module_global", features=features_for_api(sdk, api), id=api)

    findings = [finding("messages.stream"), finding("embeddings", "openai"), finding("legacy ChatCompletion", "openai")]
    plan = build_plan(findings, MODELS, "super", {f.id: "ultra" for f in findings})
    assert all(plan[f.id].keep for f in findings), {f.id: plan[f.id].model for f in findings}
    assert "not supported by the runtime" in plan["messages.stream"].reason


# --- Anthropic's messages.parse: neither stand-in has it, so the call stays ------------------------------------------


def test_an_anthropic_messages_parse_call_stays_where_it_is(tmp_path):
    files = {
        "app.py": '''from anthropic import Anthropic
from pydantic import BaseModel

client = Anthropic()


class Label(BaseModel):
    label: str


def label(text):
    return client.messages.parse(model="claude-sonnet-5-5", max_tokens=50, messages=[{"role": "user", "content": text}], output_format=Label)


def ask(text):
    return client.messages.create(model="claude-sonnet-5-5", max_tokens=50, messages=[{"role": "user", "content": text}])
''',
        "web/label.ts": '''import Anthropic from "@anthropic-ai/sdk";

const claude = new Anthropic();

export async function label(text: string) {
  return claude.messages.parse({ model: "claude-sonnet-5-5", max_tokens: 50, messages: [{ role: "user", content: text }] });
}
''',
    }
    result, sites = port(write(tmp_path / "repo", files), tmp_path / "out", app__label="super", label="super")  # even assigned
    for site_id in ("app.label", "label"):
        assert sites[site_id].status == "kept" and "messages.parse" in sites[site_id].reason, sites[site_id].reason
    assert sites["app.ask"].status == "ported" and "client = Anthropic()\nclient_nemotron = anthropic_client()\n" in (result.output / "app.py").read_text()
    assert (result.output / "web/label.ts").read_text() == files["web/label.ts"]


# --- 17. Anthropic's stand-in is built without a key, as `Anthropic()` is -------------------------------------------


def test_the_anthropic_stand_in_builds_without_a_key(monkeypatch):
    from openport.runtime import openport_llm

    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    assert isinstance(openport_llm.anthropic_client(), AnthropicOnNemotron)
    assert isinstance(openport_llm.async_anthropic_client(timeout=5), openport_llm.AsyncAnthropicOnNemotron)


# --- 19. Test files by every name JavaScript tooling gives them ----------------------------------------------------


def test_javascript_test_files_by_every_name_are_tests():
    for name in ("button.spec.tsx", "button.test.jsx", "api.test.mjs", "api.spec.mjs", "api.test.cjs", "api.spec.jsx", "x.test.ts"):
        assert is_test_path(f"src/{name}"), name
    assert not is_test_path("src/contest.ts") and not is_test_path("src/spec.ts")


# --- Round 2: where the runtime goes, for packages nested in folders that aren't, and folders with no __init__.py ---


CODEX_BLOCK = '''from openai import OpenAI

client = OpenAI()


def ask(q):
    return client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": q}])
'''


def test_the_runtime_goes_in_the_package_python_imports_not_a_folder_above_it(tmp_path):
    """AutoGPT's backend: autogpt_platform/ has an __init__.py, autogpt_platform/backend/ doesn't, and the package
    the app imports is the `backend` inside it. A relative import may not climb past that."""
    repo = write(tmp_path / "repo", {
        "autogpt_platform/__init__.py": "",
        "autogpt_platform/backend/pyproject.toml": "[project]\nname = 'backend'\n",
        "autogpt_platform/backend/backend/__init__.py": "",
        "autogpt_platform/backend/backend/blocks/__init__.py": "",
        "autogpt_platform/backend/backend/blocks/codex.py": CODEX_BLOCK,
    })
    result, sites = port(repo, tmp_path / "out")
    assert [s.status for s in sites.values()] == ["ported"]
    assert [path for path in result.added if path.endswith("openport_llm.py")] == ["autogpt_platform/backend/backend/openport_llm.py"]
    codex = (result.output / "autogpt_platform/backend/backend/blocks/codex.py").read_text()
    assert "from ..openport_llm import token_factory_client" in codex
    ran = run_python(result.output / "autogpt_platform/backend", "import backend.blocks.codex as c; print(c.client.base_url)")
    assert ran.returncode == 0, ran.stderr
    assert "tokenfactory" in ran.stdout


NAMESPACE_APP = {
    "app/main.py": "from app.llm import ask\n\nprint(ask.__name__)\n",
    "app/llm.py": '''from anthropic import Anthropic

client = Anthropic()


def ask(q):
    return client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}])
''',
}


def test_a_folder_without_an_init_still_finds_the_runtime_however_its_imported(tmp_path):
    """app/ has no __init__.py: Python imports it as a namespace package (`import app.main` from the repo), or a file
    in it runs as a script (`python llm.py` from app/). The runtime beside it is found both ways."""
    result, sites = port(write(tmp_path / "repo", NAMESPACE_APP), tmp_path / "out")
    assert [s.status for s in sites.values()] == ["ported"]
    assert [path for path in result.added if path.endswith("openport_llm.py")] == ["app/openport_llm.py"]
    as_package = run_python(result.output, "import app.main, app.llm; print(type(app.llm.client).__name__)")
    assert as_package.returncode == 0, as_package.stderr
    assert as_package.stdout.split() == ["ask", "AnthropicOnNemotron"]
    as_script = run_python(result.output / "app", "import llm; print(type(llm.client).__name__)")
    assert as_script.returncode == 0, as_script.stderr
    assert as_script.stdout.strip() == "AnthropicOnNemotron"


FIELD_CLAUDE = '''import Anthropic from "@anthropic-ai/sdk";

export class Labeller {
  client!: Anthropic;

  constructor() {
    this.client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
  }

  async label(text: string): Promise<string> {
    const message = await this.client.messages.create({ model: "claude-sonnet-5-5", max_tokens: 64, messages: [{ role: "user", content: text }] });
    return message.content[0].text;
  }
}
'''


@needs_tsc
def test_the_stand_in_fits_a_field_typed_as_the_anthropic_client(tmp_path):
    """lobehub declares `client!: Anthropic`. The real SDK's client has far more members than the stand-in, so the
    stand-in is typed loosely: assigning it there still type-checks."""
    sdk = TS_PROJECT["node_modules/@anthropic-ai/sdk/index.d.ts"].replace(
        "  beta: { messages: Messages };\n", "  beta: { messages: Messages };\n  models: { list(): Promise<unknown> };\n")
    files = {**{k: v for k, v in TS_PROJECT.items() if k != "src/claude.ts"}, "node_modules/@anthropic-ai/sdk/index.d.ts": sdk,
             "src/labeller.ts": FIELD_CLAUDE}
    root = write(tmp_path / "repo", files)
    assert tsc(root).returncode == 0, tsc(root).stdout
    result, sites = port(root, tmp_path / "out")
    assert [s.status for s in sites.values()] == ["ported"]
    assert "this.client = new AnthropicOnNemotron(" in (result.output / "src/labeller.ts").read_text()
    shutil.copytree(root / "node_modules", result.output / "node_modules")
    checked = tsc(result.output)
    assert checked.returncode == 0, checked.stdout


def test_the_runtime_import_has_the_extension_the_projects_resolution_needs(tmp_path):
    """A file with no relative imports of its own: its tsconfig.json (or the one that extends) says whether Node's
    resolution needs one. A bundler's doesn't, so none is added there."""
    from openport.port.ts_rewrite import _node_resolution, _TsRepo

    root = write(tmp_path / "repo", {
        "tsconfig.base.json": '{"compilerOptions": {"module": "NodeNext"}}',
        "api/tsconfig.json": '{\n  // comments are allowed here\n  "extends": "../tsconfig.base",\n  "include": ["src"]\n}',
        "api/src/llm.ts": "",
        "tools/tsconfig.json": '{"compilerOptions": {"moduleResolution": "nodenext", "allowImportingTsExtensions": true}}',
        "tools/llm.ts": "",
        "web/tsconfig.json": '{"compilerOptions": {"moduleResolution": "bundler"}}',
        "web/src/llm.ts": "",
        "loose.ts": "",
    })
    repo = _TsRepo(root)
    assert [_node_resolution(repo, path) for path in ("api/src/llm.ts", "tools/llm.ts", "web/src/llm.ts", "loose.ts")] == [
        ".js", ".ts", None, None]


DEEP = " + ".join(['"a"'] * 3000)


def test_a_file_too_deep_to_parse_doesnt_stop_the_port(tmp_path, monkeypatch):
    """A generated module with a 3,000-term expression mentions the model constant: Python can't parse it, so
    OpenPort can't tell what it reads, and leaves the constant to it rather than crashing. (How deep is too deep
    depends on the platform's C stack: Linux's parses this one, Windows' doesn't. The parse is made to fail on both.)"""
    from openport.port import python_rewrite

    parse = python_rewrite._File.parse.__func__

    def too_deep(cls, root, path):
        if path.endswith("big.py"):
            raise RecursionError("maximum recursion depth exceeded during ast construction")
        return parse(cls, root, path)

    monkeypatch.setattr(python_rewrite._File, "parse", classmethod(too_deep))
    repo = write(tmp_path / "repo", {
        "app/__init__.py": "",
        "app/llm.py": 'from openai import OpenAI\n\nMODEL = "gpt-4o"\nclient = OpenAI()\n\n\ndef ask(q):\n'
                      '    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": q}])\n',
        "app/big.py": f"# MODEL\nBLOB = {DEEP}\n",
    })
    result, sites = port(repo, tmp_path / "out")
    assert [s.status for s in sites.values()] == ["ported"]
    llm = (result.output / "app/llm.py").read_text()
    assert 'MODEL = "gpt-4o"' in llm and 'model="nvidia/super-test"' in llm  # the call names its model; the constant stays
    assert (result.output / "app/big.py").read_text() == (repo / "app/big.py").read_text()


def test_a_javascript_file_too_deep_to_walk_doesnt_stop_the_port(tmp_path):
    repo = write(tmp_path / "repo", {
        "package.json": '{"name": "app", "type": "module"}',
        "src/llm.ts": 'import OpenAI from "openai";\n\nconst MODEL = "gpt-4o";\nconst client = new OpenAI();\n\n'
                      'export async function ask(q: string) {\n'
                      '  return client.chat.completions.create({ model: MODEL, messages: [{ role: "user", content: q }] });\n}\n',
        "src/big.js": f"// MODEL\nexport const BLOB = {DEEP};\n",
    })
    result, sites = port(repo, tmp_path / "out")
    assert [s.status for s in sites.values()] == ["ported"]
    assert "nvidia/super-test" in (result.output / "src/llm.ts").read_text()


SHADOWED = '''from openai import OpenAI
from vendor_sdk import connect

client = OpenAI()


def per_user(key, q):
    client = connect(key)  # the user's own client, not the module's
    return client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": q}])


def through_beta(key, q):
    client = connect(key)
    return client.beta.chat.completions.parse(model="gpt-4o-mini", messages=[{"role": "user", "content": q}])


def shared(q):
    return client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": q}])
'''


def test_a_function_that_rebinds_the_clients_name_isnt_given_the_module_client(tmp_path):
    result, sites = port(write(tmp_path / "repo", {"app.py": SHADOWED}), tmp_path / "out")
    assert sites["app.shared"].status == "ported"
    assert sites["app.per_user"].status == sites["app.through_beta"].status == "kept"
    code = (result.output / "app.py").read_text()
    assert code.count('model="gpt-4o-mini"') == 2  # the two calls on the user's client name their model as before


CLOSES = '''from anthropic import Anthropic

client = Anthropic()


def ask(q):
    return client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}])


def shutdown():
    client.close()
'''

READS_URL = '''from anthropic import Anthropic
import httpx

client = Anthropic()


def ask(q):
    return client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": q}])


def models(key):
    return httpx.get(f"{client.base_url}/v1/models", headers={"x-api-key": key}).json()
'''


def test_closing_a_client_is_no_reason_to_keep_it_but_reading_its_url_is(tmp_path):
    """`client.close()` sends nothing: the client itself moves, and the stand-in closes. Its base URL is another
    matter: lobehub hands `this.client.baseURL` to its own fetch of /v1/models, with the Anthropic key, which would
    then go to Token Factory. So the client that's read stays, and the calls move to a second one beside it."""
    result, sites = port(write(tmp_path / "repo", {"app.py": CLOSES}), tmp_path / "out")
    assert sites["app.ask"].status == "ported", sites["app.ask"].reason
    code = (result.output / "app.py").read_text()
    assert "client = anthropic_client()" in code and "_nemotron" not in code and "Anthropic()" not in code
    ran = run_python(result.output, "import app; app.shutdown(); print('closed')")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "closed"
    result, sites = port(write(tmp_path / "repo2", {"app.py": READS_URL}), tmp_path / "out2")
    code = (result.output / "app.py").read_text()
    assert sites["app.ask"].status == "ported" and "client = Anthropic()\nclient_nemotron = anthropic_client()\n" in code
    assert 'httpx.get(f"{client.base_url}/v1/models"' in code  # still Anthropic's URL, with Anthropic's key


def test_the_async_stand_in_closes_too():
    import asyncio

    from openport.runtime.openport_llm import AsyncAnthropicOnNemotron

    closed = []

    class Inner:
        async def close(self):
            closed.append(True)

    asyncio.run(AsyncAnthropicOnNemotron(client=Inner()).close())
    assert closed == [True]


def test_the_python_runtime_imports_on_the_oldest_python_an_app_may_run():
    """The runtime is copied into the app (CowAgent declares Python 3.7+). Its grammar is 3.8's, and nothing it
    evaluates at import needs 3.10: `str | Callable` outside an annotation raises TypeError on 3.9."""
    import ast

    source = (Path(__file__).parents[1] / "src" / "openport" / "runtime" / "openport_llm.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    assert "from __future__ import annotations" in source  # annotations aren't evaluated
    annotations = {id(n) for node in ast.walk(tree) for a in (getattr(node, "annotation", None), getattr(node, "returns", None))
                   if a is not None for n in ast.walk(a)}
    unions = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
              and id(n) not in annotations and isinstance(n.left, (ast.Name, ast.Subscript, ast.Attribute))
              and isinstance(n.right, (ast.Name, ast.Subscript, ast.Attribute, ast.Constant))]
    assert unions == []


def test_a_beta_call_on_a_shared_factory_moves_to_the_second_one(tmp_path):
    """The factory split, for a call through `get_client().beta` too: it's the same client."""
    from .test_port import PREFLIGHT_LIKE

    root = write(tmp_path / "repo", {**PREFLIGHT_LIKE, "agents/summary.py": '''from core.anthropic_client import get_client


def summarize(text):
    response = get_client().beta.messages.create(model="claude-opus-4-5", max_tokens=300, messages=[{"role": "user", "content": text}])
    return response.content[0].text
'''})
    result, sites = port(root, tmp_path / "out", review="keep")
    assert (sites["review"].status, sites["summary.summarize"].status) == ("kept", "ported"), sites["summary.summarize"].reason
    summary = (result.output / "agents/summary.py").read_text()
    assert "response = get_client_nemotron().beta.messages.create(model=\"nvidia/super-test\"" in summary


def test_openai_isnt_added_where_it_would_break_the_install(tmp_path):
    """pip refuses a requirements file where one line has no hash once any has one, and `uv sync --locked` fails when
    pyproject.toml lists what uv.lock doesn't. Those are left for the app to add with its own tools, and the note says so."""
    from .test_port_review import CLAUDE_ONLY

    hashed = {"app.py": CLAUDE_ONLY, "requirements.txt": "anthropic==0.40.0 \\n    --hash=sha256:" + "a" * 64 + "\n"}
    result, sites = port(write(tmp_path / "hashed", hashed), tmp_path / "out1")
    assert "requirements.txt" not in result.changed
    assert any("`openai` package" in note and "hashes" in note for note in sites["app.ask"].notes)

    for lock in ("uv.lock", "poetry.lock", "pdm.lock"):
        locked = {"app.py": CLAUDE_ONLY, "pyproject.toml": '[project]\nname = "x"\ndependencies = ["anthropic>=0.40"]\n', lock: "# locked\n"}
        result, sites = port(write(tmp_path / lock, locked), tmp_path / f"out-{lock}")
        assert "pyproject.toml" not in result.changed, lock
        assert any("`openai` package" in note and "lockfile" in note for note in sites["app.ask"].notes), lock


def test_an_apps_own_runs_folder_is_scanned_and_copied(tmp_path, monkeypatch):
    """`openport port .` keeps its output in runs/ in the repo. An app with a runs/ of its own (a training script) keeps
    it: only OpenPort's folders and reports there are left out."""
    repo = write(tmp_path / "repo", {**APP, "runs/train.py": APP["app.py"].replace("def ask", "def train")})
    monkeypatch.setattr(config, "RUNS_DIR", repo / "runs")
    write(repo / "runs" / "repos" / "someone__else", {"other.py": APP["app.py"]})  # a clone OpenPort fetched
    (repo / "runs" / "tune-repo-20261029-120000.json").write_text("{}")
    report = scan_repo(repo)
    assert sorted(f.file for f in report.findings) == ["app.py", "runs/train.py"]
    for _ in range(2):  # and again, with the first port's copy now in runs/ports
        result = port_repo(repo, scan_repo(repo), build_plan(scan_repo(repo).findings, MODELS), repo / "runs" / "ports" / "repo")
        assert sorted(p.relative_to(result.output / "runs").as_posix() for p in (result.output / "runs").rglob("*")) == [
            "openport_llm.py", "train.py"]  # the app's script and its runtime: no clones, ports or reports
        assert {s.id: s.status for s in result.sites} == {"app.ask": "ported", "train": "ported"}


SHARED_PROMPT = "You are a support triage assistant. Classify the ticket by category and priority, and reply in JSON."
SHARED_PROMPT_APP = {
    "prompts/triage.txt": SHARED_PROMPT + "\n",
    "app.py": '''from pathlib import Path

from openai import OpenAI

client = OpenAI()
PROMPT = Path(__file__).with_name("prompts").joinpath("triage.txt").read_text()


def triage(ticket):
    return client.chat.completions.create(model="gpt-5", messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": ticket}])


def escalate(ticket):
    return client.chat.completions.create(model="gpt-5", messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": ticket}])
''',
}


def test_a_prompt_file_a_call_that_stays_reads_isnt_given_another_calls_addendum(tmp_path):
    tuned = {"model": "nano", "variant": {"system_addendum": "Answer with the JSON object only."}, "system_prompts": [SHARED_PROMPT]}
    kept = {"model": "keep", "reason": "no Nemotron tier reached 90%", "system_prompts": [SHARED_PROMPT]}
    result, sites = port(write(tmp_path / "repo", SHARED_PROMPT_APP), tmp_path / "out", app__triage=tuned, app__escalate=kept)
    assert (sites["app.triage"].status, sites["app.escalate"].status) == ("ported", "kept")
    assert (result.output / "prompts/triage.txt").read_text() == SHARED_PROMPT + "\n"  # escalate still reads it as it was
    assert any("a call that stays uses the prompt in prompts/triage.txt too" in note for note in sites["app.triage"].notes)

    alone = {**SHARED_PROMPT_APP, "app.py": SHARED_PROMPT_APP["app.py"].split("\n\ndef escalate")[0] + "\n"}
    result, sites = port(write(tmp_path / "alone", alone), tmp_path / "out2", app__triage=tuned)
    assert (result.output / "prompts/triage.txt").read_text() == SHARED_PROMPT + "\n\nAnswer with the JSON object only.\n"


def test_the_plan_keeps_the_prompts_a_kept_call_sent():
    from types import SimpleNamespace

    from openport.evals.spec import Spec
    from openport.tune import export_plan

    kept = SimpleNamespace(site_id="escalate", file="app.py", function="escalate", chosen=None, best=None, attempts=[], error=None,
                           system_prompts=[SHARED_PROMPT])
    plan = export_plan(Spec(target="app", source_root=Path("does-not-exist"), call_sites=[]), [kept], 0.9)["call_sites"]
    assert plan["escalate"]["model"] == "keep" and plan["escalate"]["system_prompts"] == [SHARED_PROMPT]


def test_a_calls_own_timeout_reaches_token_factory():
    """`messages.create(..., timeout=5)` is the app's latency budget for that call: it goes with the request. The
    provider's own per-call headers and query parameters mean nothing to Token Factory and stay behind."""
    fake = FakeTokenFactory(lambda request: completion(content="ok"))
    client = AnthropicOnNemotron(client=fake)
    client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[{"role": "user", "content": "hi"}], timeout=5,
                           extra_headers={"anthropic-beta": "x"})
    assert fake.requests[-1]["timeout"] == 5 and "extra_headers" not in fake.requests[-1]


@needs_node
def test_the_javascript_stand_in_honours_a_calls_abort_signal_and_timeout(tmp_path):
    """lobehub passes `{ signal }` as create's second argument to cancel a chat the user stopped."""
    result = run_node(tmp_path, """
const seen = [];
globalThis.fetch = async (url, init) => {
  seen.push(init.signal ? "signal" : "none");
  await new Promise((resolve, reject) => {
    if (init.signal?.aborted) return reject(init.signal.reason);
    init.signal?.addEventListener("abort", () => reject(init.signal.reason));
    setTimeout(resolve, 200);
  });
  return new Response(JSON.stringify({ choices: [{ message: { content: "ok" }, finish_reason: "stop" }], usage: {} }));
};
const client = new runtime.AnthropicOnNemotron({ apiKey: "k" });
const params = { model: "m", max_tokens: 5, messages: [{ role: "user", content: "hi" }] };
const stop = new AbortController();
const stopped = client.messages.create(params, { signal: stop.signal }).then(() => "finished", (e) => "aborted");
stop.abort();
const timedOut = await client.messages.create(params, { timeout: 20 }).then(() => "finished", () => "aborted");
const plain = await client.messages.create(params).then((m) => m.content[0].text);
console.log(JSON.stringify({ stopped: await stopped, timedOut, plain, seen }));
""")
    assert result == {"stopped": "aborted", "timedOut": "aborted", "plain": "ok", "seen": ["signal", "signal", "none"]}
