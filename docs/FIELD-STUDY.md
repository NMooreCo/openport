# Field study: OpenPort on 20 popular open-source AI apps

What OpenPort finds in the AI apps people actually run, how much of it it can test as written, and how much of their dependency tree it can upgrade. Run on Oct 24, 2026.

## How the apps were chosen

The 20 most-starred applications and end-user tools on GitHub tagged `openai`, `anthropic`, `claude` or `chatgpt`, written in Python or TypeScript (GitHub search, Oct 24, 2026). Left out: SDKs and frameworks (LangChain, LiteLLM, AutoGen, mem0), model servers and training tools (vLLM, Unsloth), API gateways and unofficial API clients (OmniRoute, gpt4free), and collections of prompts, skills or links.

Everything was static. Each repo was shallow-cloned and read, never run, by OpenPort's own scanner and its upgrade dry run, with OSV.dev's advisories as of that day. [`scripts/field_study.py`](../scripts/field_study.py) reproduces it.

## What I found

- **180 OpenAI and Anthropic SDK calls in 11 of the 20 apps:** 112 on OpenAI's SDK and 68 on Anthropic's, 149 in Python and 31 in TypeScript. 46 use tool calling and 14 ask for JSON.
- **50 of them (28%) can be tested as written.** Of the rest, 58 live in class methods, 45 reach their client through an attribute, a factory or a local variable that can't be swapped at import, and 19 are TypeScript functions that aren't exported. Those need a small adapter, like the one in OpenPort's [eval pack for preflight](../examples/evalpacks/preflight).
- **Most apps also reach models without either SDK.** 17 of the 20 have code that names an OpenAI- or Anthropic-style endpoint (`/v1/chat/completions`, `api.anthropic.com/v1`) directly; 3 use LiteLLM, 4 LangChain and 2 the Vercel AI SDK.
- **19 of the 20 lock a dependency version with a known vulnerability:** 746 vulnerable versions out of 27,867 locked, with 3,302 advisories between them. 687 of those (92%) have a release that fixes at least one advisory and is at least a week old, 652 of them fixing every one. 37 are held back because the only fix is newer than a week, which is the supply-chain guard at work, and 22 have no fix yet.
- **It's fast enough to run on a whim.** The largest repo here, lobehub's 18,195 files, scans in under 10 seconds; checking ruflo's 3,264 locked versions against OSV took two minutes.

## Per app

| Repo | Stars | Files | LLM calls | SDKs | Testable as written | Also reaches models via | Locked versions | Vulnerable | With a fix ≥ 7 days old |
|---|---:|---:|---:|---|---:|---|---:|---:|---:|
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 256,806 | 15,307 | 33 | 31 OpenAI + 2 Anthropic | 3 | HTTP, LangChain | 2,627 | 11 | 7 |
| [microsoft/markitdown](https://github.com/microsoft/markitdown) | 191,973 | 93 | 3 | 3 OpenAI | 0 | – | 0 | 0 | 0 |
| [Significant-Gravitas/AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) | 187,729 | 6,988 | 29 | 26 OpenAI + 3 Anthropic | 1 | HTTP | 2,514 | 114 | 107 |
| [langgenius/dify](https://github.com/langgenius/dify) | 159,437 | 11,629 | 0 | – | 0 | HTTP | 626 | 11 | 6 |
| [open-webui/open-webui](https://github.com/open-webui/open-webui) | 156,658 | 421 | 0 | – | 0 | HTTP | 1,600 | 69 | 61 |
| [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | 155,985 | 8,397 | 14 | 14 OpenAI | 0 | HTTP, LiteLLM, LangChain | 3,184 | 41 | 27 |
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | 106,951 | 8,998 | 0 | – | 0 | HTTP, Vercel AI SDK | 1,795 | 38 | 37 |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 102,817 | 2,026 | 0 | – | 0 | HTTP | 795 | 15 | 14 |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | 91,748 | 2,315 | 0 | – | 0 | HTTP | 1,453 | 32 | 30 |
| [ChatGPTNextWeb/NextChat](https://github.com/ChatGPTNextWeb/NextChat) | 88,890 | 190 | 0 | – | 0 | HTTP | 1,145 | 51 | 51 |
| [lobehub/lobehub](https://github.com/lobehub/lobehub) | 83,470 | 18,195 | 16 | 12 OpenAI + 4 Anthropic | 0 | HTTP | 1,732 | 23 | 17 |
| [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) | 79,159 | 114 | 49 | 49 Anthropic | 39 | – | 235 | 8 | 8 |
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo) | 79,038 | 4,341 | 6 | 5 OpenAI + 1 Anthropic | 5 | HTTP | 3,264 | 230 | 227 |
| [headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom) | 77,729 | 1,856 | 26 | 17 OpenAI + 9 Anthropic | 2 | HTTP, LiteLLM, LangChain, Vercel AI SDK | 1,604 | 10 | 10 |
| [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) | 71,581 | 447 | 0 | – | 0 | – | 243 | 6 | 6 |
| [binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic) | 71,540 | 293 | 2 | 2 OpenAI | 0 | HTTP | 21 | 4 | 3 |
| [code-yeongyu/oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) | 70,313 | 9,515 | 0 | – | 0 | HTTP | 1,575 | 19 | 18 |
| [Aider-AI/aider](https://github.com/Aider-AI/aider) | 49,739 | 160 | 0 | – | 0 | HTTP, LiteLLM | 200 | 30 | 28 |
| [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) | 47,562 | 1,133 | 1 | 1 OpenAI | 0 | HTTP | 557 | 31 | 29 |
| [LibreChat-AI/LibreChat](https://github.com/LibreChat-AI/LibreChat) | 46,258 | 6,580 | 1 | 1 OpenAI | 0 | HTTP, LangChain | 2,697 | 3 | 1 |

"HTTP" means files that name an OpenAI- or Anthropic-style endpoint, which includes some configuration as well as calls. "Vulnerable" counts dependency versions: a package locked at two versions counts twice. markitdown locks nothing (it ships a `pyproject.toml` without a lockfile).

## What it changed in OpenPort

The study found five problems in OpenPort, all fixed the same day:

- **Scans of large repos took minutes** (hermes-agent's 15,307 files: three). OpenPort now parses only the files that can hold a call. It finds the same 180 calls, and the 19 repos it had scanned before the change took 27 seconds instead of 17 minutes.
- **Advisories were looked up one dependency at a time**, and a Node app's lockfiles list thousands. OSV's batch API now takes 1,000 per request.
- **Most of these apps are monorepos or lock with yarn or bun**, so the upgrader saw a fraction of what they install. It now reads every lockfile in the repo, `yarn.lock` and `bun.lock` included.
- **AutoGPT wouldn't clone on Windows** (paths over 260 characters) and left a broken folder that later scans would have reused. Clones now allow long paths and never leave a half-done folder behind.
- **The repos' own invalid escape sequences** printed Python warnings during the scan. They're silenced now.

## What it says to build next

- **Plain HTTP calls to OpenAI-compatible endpoints.** They're the most common way these apps reach a model, and the easiest to move to Token Factory: a base URL and a model name.
- **Testing calls in class methods without an adapter**, by intercepting the SDK itself rather than the client object.
- **Updating `yarn.lock`, `bun.lock` and subprojects' lockfiles**, not just reading them.

## Reproduce

```bash
uv run python scripts/field_study.py --advisories NousResearch/hermes-agent lobehub/lobehub   # any repos
uv run openport scan NousResearch/hermes-agent        # one repo's calls, as a table
uv run openport upgrade NousResearch/hermes-agent     # its vulnerable dependencies (a dry run: nothing is run)
```
