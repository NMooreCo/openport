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
    assert main.startswith('"""Answers questions."""\nfrom openport_llm import anthropic_client\n\nclient = anthropic_client(timeout=10)\n')
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
