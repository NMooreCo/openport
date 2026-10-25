# Field study: OpenPort on 20 popular open-source AI apps

What OpenPort finds in the AI apps people actually run, how much of it it can test as written, and how much of their dependency tree it can upgrade. Run on Oct 24, 2026.

## How the apps were chosen

The 20 most-starred applications and end-user tools on GitHub tagged `openai`, `anthropic`, `claude` or `chatgpt`, written in Python or TypeScript (GitHub search, Oct 24, 2026). Left out: SDKs and frameworks (LangChain, LiteLLM, AutoGen, mem0), model servers and training tools (vLLM, Unsloth), API gateways and unofficial API clients (OmniRoute, gpt4free), and collections of prompts, skills or links.

Everything was static. Each repo was shallow-cloned and read, never run, by OpenPort's own scanner and its upgrade dry run, with OSV.dev's advisories as of that day. [`scripts/field_study.py`](../scripts/field_study.py) reproduces it.

## What I found

- **220 LLM calls in 14 of the 20 apps' own code:** 132 through the OpenAI and Anthropic SDKs, in 11 apps (72 OpenAI, 60 Anthropic), and 88 plain HTTP calls to OpenAI- or Anthropic-style endpoints, in 9. 161 are in Python and 59 in TypeScript; 51 use tool calling. Another 943 sit in tests, docs' code samples, benchmarks and evals, which OpenPort lists apart.
- **47 of the 132 SDK calls (36%) can be tested as written.** Of the rest, 30 live in class methods, 35 reach their client through an attribute, a factory or a local variable that can't be swapped at import, and 13 are TypeScript functions that aren't exported. Those need a small adapter, like the one in OpenPort's [eval pack for preflight](../examples/evalpacks/preflight).
- **The 88 HTTP calls are reported with how to move them** (point the URL at Token Factory, pick a Nemotron model), but OpenPort doesn't rewrite or test them yet. A few more apps name an endpoint in their code but build the call from another file, which the scanner doesn't follow. 3 apps also use LiteLLM, 4 LangChain and 2 the Vercel AI SDK.
- **19 of the 20 lock a dependency version with a known vulnerability:** 746 vulnerable versions out of 27,867 locked, with 3,302 advisories between them. 687 of those (92%) have a release that fixes at least one advisory and is at least a week old, 652 of them fixing every one. 37 are held back because the only fix is newer than a week, which is the supply-chain guard at work, and 22 have no fix yet.
- **It's fast enough to run on a whim.** The largest repo here, lobehub's 18,195 files, scans in under 10 seconds; checking ruflo's 3,264 locked versions against OSV took two minutes.

## Per app

| Repo | Stars | Files | SDK calls | HTTP calls | Testable as written | Also reaches models via | Locked versions | Vulnerable | With a fix ≥ 7 days old |
|---|---:|---:|---|---:|---:|---|---:|---:|---:|
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 256,806 | 15,307 | 14 OpenAI | 4 | 2 | LangChain | 2,627 | 11 | 7 |
| [microsoft/markitdown](https://github.com/microsoft/markitdown) | 191,973 | 93 | 3 OpenAI | – | 0 | – | 0 | 0 | 0 |
| [Significant-Gravitas/AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) | 187,729 | 6,988 | 26 OpenAI + 3 Anthropic | 1 | 1 | – | 2,514 | 114 | 107 |
| [langgenius/dify](https://github.com/langgenius/dify) | 159,437 | 11,629 | – | – | 0 | – | 626 | 11 | 6 |
| [open-webui/open-webui](https://github.com/open-webui/open-webui) | 156,658 | 421 | – | 5 | 0 | – | 1,600 | 69 | 61 |
| [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | 155,985 | 8,397 | 2 OpenAI | – | 0 | LiteLLM, LangChain | 3,184 | 41 | 27 |
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | 106,951 | 8,998 | – | 2 | 0 | Vercel AI SDK | 1,795 | 38 | 37 |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 102,817 | 2,026 | – | – | 0 | – | 795 | 15 | 14 |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | 91,748 | 2,315 | – | – | 0 | – | 1,453 | 32 | 30 |
| [ChatGPTNextWeb/NextChat](https://github.com/ChatGPTNextWeb/NextChat) | 88,890 | 190 | – | – | 0 | – | 1,145 | 51 | 51 |
| [lobehub/lobehub](https://github.com/lobehub/lobehub) | 83,470 | 18,195 | 12 OpenAI + 4 Anthropic | 4 | 0 | – | 1,732 | 23 | 17 |
| [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) | 79,159 | 114 | 49 Anthropic | – | 39 | – | 235 | 8 | 8 |
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo) | 79,038 | 4,341 | 5 OpenAI | 26 | 5 | – | 3,264 | 230 | 227 |
| [headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom) | 77,729 | 1,856 | 6 OpenAI + 4 Anthropic | 3 | 0 | LiteLLM, LangChain, Vercel AI SDK | 1,604 | 10 | 10 |
| [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) | 71,581 | 447 | – | – | 0 | – | 243 | 6 | 6 |
| [binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic) | 71,540 | 293 | 2 OpenAI | – | 0 | – | 21 | 4 | 3 |
| [code-yeongyu/oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) | 70,313 | 9,515 | – | 1 | 0 | – | 1,575 | 19 | 18 |
| [Aider-AI/aider](https://github.com/Aider-AI/aider) | 49,739 | 160 | – | – | 0 | LiteLLM | 200 | 30 | 28 |
| [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) | 47,562 | 1,133 | 1 OpenAI | 42 | 0 | – | 557 | 31 | 29 |
| [LibreChat-AI/LibreChat](https://github.com/LibreChat-AI/LibreChat) | 46,258 | 6,580 | 1 OpenAI | – | 0 | LangChain | 2,697 | 3 | 1 |

Calls count only the app's own code, not its tests, docs' samples, benchmarks or evals. "Vulnerable" counts dependency versions: a package locked at two versions counts twice. markitdown locks nothing (it ships a `pyproject.toml` without a lockfile).

## What it changed in OpenPort

The study found seven problems in OpenPort, all fixed the same day:

- **It missed the commonest way these apps reach a model: plain HTTP.** The scanners now find requests, httpx and aiohttp posts in Python, and fetch, axios, ky and got posts in TypeScript, whose URL names an LLM endpoint. They read the model, tools, JSON mode and streaming from the payload, and leave out server routes and OpenAI's non-chat paths.
- **Docs' code samples, benchmarks and evals counted as the app's calls.** Langflow's docs alone held 108 of them. They're listed apart now, like tests.
- **Scans of large repos took minutes** (hermes-agent's 15,307 files: three). OpenPort now parses only the files that can hold a call. It found the same calls, and the 19 repos it had scanned before the change took 27 seconds instead of 17 minutes.
- **Advisories were looked up one dependency at a time**, and a Node app's lockfiles list thousands. OSV's batch API now takes 1,000 per request.
- **Most of these apps are monorepos or lock with yarn or bun**, so the upgrader saw a fraction of what they install. It now reads every lockfile in the repo, `yarn.lock` and `bun.lock` included.
- **AutoGPT wouldn't clone on Windows** (paths over 260 characters) and left a broken folder that later scans would have reused. Clones now allow long paths and never leave a half-done folder behind.
- **The repos' own invalid escape sequences** printed Python warnings during the scan. They're silenced now.

## What it says to build next

- **Rewriting and testing plain HTTP calls**, now that they're found. An OpenAI-style one moves to Token Factory with a URL and a model name.
- **Testing calls in class methods without an adapter**, by intercepting the SDK itself rather than the client object.
- **Updating `yarn.lock`, `bun.lock` and subprojects' lockfiles**, not just reading them.

## Reproduce

```bash
uv run python scripts/field_study.py --advisories NousResearch/hermes-agent lobehub/lobehub   # any repos
uv run openport scan NousResearch/hermes-agent        # one repo's calls, as a table
uv run openport upgrade NousResearch/hermes-agent     # its vulnerable dependencies (a dry run: nothing is run)
```
