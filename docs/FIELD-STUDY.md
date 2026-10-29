# Field study: OpenPort on 20 popular open-source AI apps

What OpenPort finds in the AI apps people actually run, how much of it it can test as written, how much its port moves to Nemotron, and how much of their dependency tree it can upgrade. Run on Oct 24, 2026, and run again after each round of fixes it led to.

## How the apps were chosen

The 20 most-starred applications and end-user tools on GitHub tagged `openai`, `anthropic`, `claude` or `chatgpt`, written in Python or TypeScript (GitHub search, Oct 24, 2026). Left out: SDKs and frameworks (LangChain, LiteLLM, AutoGen, mem0), model servers and training tools (vLLM, Unsloth), API gateways and unofficial API clients (OmniRoute, gpt4free), and collections of prompts, skills or links.

Everything was static. Each repo was shallow-cloned and read, never run, by OpenPort's own scanner, its port (which worked out each rewrite without writing it) and its upgrade dry run, with OSV.dev's advisories as of that day. [`scripts/field_study.py`](../scripts/field_study.py) reproduces it.

## What I found

- **207 LLM calls in 14 of the 20 apps' own code:** 132 through the OpenAI and Anthropic SDKs, in 11 apps (72 OpenAI, 60 Anthropic), and 75 plain HTTP calls to OpenAI- or Anthropic-style endpoints, in 9. 151 are in Python and 56 in TypeScript; 51 use tool calling. Another 943 sit in tests, docs' code samples, benchmarks and evals, which OpenPort lists apart.
- **160 of the 207 calls (77%) can be tested as written**, once the app's own dependencies are installed: wherever their client comes from, in a function or in a class's method, and over plain HTTP too, whose requests OpenPort redirects to its stand-in for the API. Most of the rest are in methods of classes OpenPort can't build on its own (20): their constructors take arguments, which the spec can give as `init:`, or come from a base class it doesn't follow. Another 16 are in TypeScript functions inside other functions or objects, where the harness can't reach them.
- **`openport port` moves 79 of them (38%) to Nemotron as they are:** 66 SDK calls and 13 HTTP calls. Each of the other 128 stays where it is with the reason, and most reasons are a choice the code makes somewhere OpenPort doesn't rewrite:
  - 43 get their client from code the port doesn't rewrite: 22 are handed it, by their caller (19) or as their object is built (3), from code out of its reach: a library's user (markitdown), a layer that routes between providers (11 of hermes-agent's), or TypeScript, where the port doesn't follow clients across calls yet (3); 12 use a client of another class, such as Groq's SDK; 5 inherit it from a class outside the app; the port can't trace the last 4;
  - 42 HTTP calls build their URL from settings or variables;
  - 20 pick their provider at runtime: a factory choosing between providers, a client built from its caller's settings, or a table of endpoints;
  - 22 are calls Nemotron on Token Factory can't serve as written: Anthropic's own HTTP format (10), embeddings (7), audio (3), the provider's own web search (1) and a streamed Anthropic response (1);
  - 1 is a plain HTTP call to OpenAI's Responses API, which Token Factory documents but the port doesn't rewrite yet.

  Images no longer keep a call where it is: a call that sends them goes to Nemotron 3.5 Super VL, the Nemotron that reads images. Of the 11 here, 1 moved (CowAgent's); the other 10 also get their client or URL from somewhere the port doesn't rewrite, and are counted above.
- **19 of the 20 lock a dependency version with a known vulnerability:** 746 vulnerable versions out of 27,867 locked, with 3,302 advisories between them. 687 of those (92%) have a release that fixes at least one advisory and is at least a week old, 652 of them fixing every one. 37 are held back because the only fix is newer than a week, which is the supply-chain guard at work, and 22 have no fix yet.
- **Most of these apps are monorepos.** AutoGPT's four `poetry.lock` files, lobehub's and claude-mem's lockfiles, and 20 of ruflo's `package-lock.json` files all sit in subprojects. `openport upgrade --project <folder>` upgrades one, with its own tests and the whole repo there for its path dependencies.
- **It's fast enough to run on a whim.** The largest repo here, lobehub's 18,195 files, scans in under 10 seconds; checking ruflo's 3,264 locked versions against OSV took two minutes. 3 apps also reach models through LiteLLM, 4 through LangChain and 2 through the Vercel AI SDK, which OpenPort doesn't rewrite.

## Per app

| Repo | Stars | Files | SDK calls | HTTP calls | Testable as written | Moved by the port | Also reaches models via | Locked versions | Vulnerable | With a fix ≥ 7 days old |
|---|---:|---:|---|---:|---:|---:|---|---:|---:|---:|
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 256,806 | 15,307 | 14 OpenAI | 4 | 16 | 2 | LangChain | 2,627 | 11 | 7 |
| [microsoft/markitdown](https://github.com/microsoft/markitdown) | 191,973 | 93 | 3 OpenAI | – | 1 | 0 | – | 0 | 0 | 0 |
| [Significant-Gravitas/AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) | 187,729 | 6,988 | 26 OpenAI + 3 Anthropic | 1 | 19 | 6 | – | 2,514 | 114 | 107 |
| [langgenius/dify](https://github.com/langgenius/dify) | 159,437 | 11,629 | – | – | 0 | 0 | – | 626 | 11 | 6 |
| [open-webui/open-webui](https://github.com/open-webui/open-webui) | 156,658 | 421 | – | 5 | 5 | 0 | – | 1,600 | 69 | 61 |
| [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | 155,985 | 8,397 | 2 OpenAI | – | 2 | 0 | LiteLLM, LangChain | 3,184 | 41 | 27 |
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | 106,951 | 8,998 | – | 2 | 2 | 0 | Vercel AI SDK | 1,795 | 38 | 37 |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 102,817 | 2,026 | – | – | 0 | 0 | – | 795 | 15 | 14 |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | 91,748 | 2,315 | – | – | 0 | 0 | – | 1,453 | 32 | 30 |
| [ChatGPTNextWeb/NextChat](https://github.com/ChatGPTNextWeb/NextChat) | 88,890 | 190 | – | – | 0 | 0 | – | 1,145 | 51 | 51 |
| [lobehub/lobehub](https://github.com/lobehub/lobehub) | 83,470 | 18,195 | 12 OpenAI + 4 Anthropic | 1 | 7 | 4 | – | 1,732 | 23 | 17 |
| [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) | 79,159 | 114 | 49 Anthropic | – | 40 | 47 | – | 235 | 8 | 8 |
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo) | 79,038 | 4,341 | 5 OpenAI | 26 | 24 | 14 | – | 3,264 | 230 | 227 |
| [headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom) | 77,729 | 1,856 | 6 OpenAI + 4 Anthropic | 3 | 9 | 5 | LiteLLM, LangChain, Vercel AI SDK | 1,604 | 10 | 10 |
| [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) | 71,581 | 447 | – | – | 0 | 0 | – | 243 | 6 | 6 |
| [binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic) | 71,540 | 293 | 2 OpenAI | – | 1 | 0 | – | 21 | 4 | 3 |
| [code-yeongyu/oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) | 70,313 | 9,515 | – | 1 | 0 | 0 | – | 1,575 | 19 | 18 |
| [Aider-AI/aider](https://github.com/Aider-AI/aider) | 49,739 | 160 | – | – | 0 | 0 | LiteLLM | 200 | 30 | 28 |
| [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) | 47,562 | 1,133 | 1 OpenAI | 32 | 33 | 1 | – | 557 | 31 | 29 |
| [LibreChat-AI/LibreChat](https://github.com/LibreChat-AI/LibreChat) | 46,258 | 6,580 | 1 OpenAI | – | 1 | 0 | LangChain | 2,697 | 3 | 1 |

Calls count only the app's own code, not its tests, docs' samples, benchmarks or evals. "Vulnerable" counts dependency versions: a package locked at two versions counts twice. markitdown locks nothing (it ships a `pyproject.toml` without a lockfile). learn-claude-code's 8 are from the Oct 28 dry run; the Oct 29 upgrade of its `web` project found 7 vulnerable versions in 6 packages, 48 advisories, and fixed them all.

## What it changed in OpenPort

Each problem the study found was fixed the same day, and the study run again. The first round:

- **Most calls couldn't be tested as written.** The test harness swapped the module attribute a call got its client from, so a client built in a function, kept on `self` or made by a factory was out of reach, async code wasn't run at all, and neither were TypeScript calls on Anthropic's SDK or plain HTTP calls. It now routes the SDKs' own calls, sync and async, while a test runs, and redirects HTTP requests to LLM endpoints to its stand-in for the API.
- **It missed the commonest way these apps reach a model: plain HTTP.** The scanners now find requests, httpx and aiohttp posts in Python, and fetch, axios, ky and got posts in TypeScript, whose URL names an LLM endpoint. They read the model, tools, JSON mode and streaming from the payload, and leave out server routes and OpenAI's non-chat paths.
- **Docs' code samples, benchmarks and evals counted as the app's calls.** Langflow's docs alone held 108 of them. They're listed apart now, like tests.
- **Scans of large repos took minutes** (hermes-agent's 15,307 files: three). OpenPort now parses only the files that can hold a call. It found the same calls, and the 19 repos it had scanned before the change took 27 seconds instead of 17 minutes.
- **Advisories were looked up one dependency at a time**, and a Node app's lockfiles list thousands. OSV's batch API now takes 1,000 per request.
- **Most of these apps are monorepos or lock with yarn or bun**, so the upgrader saw a fraction of what they install. It now reads every lockfile in the repo, `yarn.lock` and `bun.lock` included.
- **AutoGPT wouldn't clone on Windows** (paths over 260 characters) and left a broken folder that later scans would have reused. Clones now allow long paths and never leave a half-done folder behind.
- **The repos' own invalid escape sequences** printed Python warnings during the scan. They're silenced now.

The second round, from running the port itself over the 20 apps and reading every change it made:

- **HTTP calls were found, but the port left them alone.** It now moves an OpenAI-style one where the code spells out where it goes: the URL or base URL, the environment variable the key comes from, and the model, plus the tuned settings. A URL constant that calls staying behind also use gets a second constant, as a shared client is split.
- **Some of what it moved should never have moved.** Reading the diffs turned up speech to text and text to speech over chat completions (`input_audio`, audio output), a search API that answers from the web, an app posting to its own server (a host built from variables), a model server on the same machine, and a table of providers whose every entry pointed at Token Factory afterwards. Audio and the provider's web search now keep a call where it is, like images, and so do placeholder hosts, local servers and tables of endpoints.
- **Two other APIs read as Anthropic's.** Feishu's chat API also posts to `/im/v1/messages` (10 of CowAgent's calls), and a generated client for lobehub's own REST API names `/api/v1/messages` (3 more). Neither counts now: 207 calls, not 220.
- **Calls in class methods couldn't be tested without an adapter.** The harness now builds the class for each case (with `init:` from the spec when it takes arguments), and calls static methods on the class. With that, and the TypeScript functions their module doesn't export (below), 160 of the 207 calls are testable as written, up from 102 (and 47 before the first round).
- **The port didn't find clients made by factories called with arguments, kept on `this` in TypeScript, or imported in a monorepo** (from a project's own folder, not the repo root). It does now, and it keeps clients whose provider is chosen elsewhere: a factory switching between providers, like AutoGPT's OpenRouter-or-OpenAI one, or a client built from its caller's settings. Rewriting those would override a choice made outside the code it edits.
- **Three bugs in its TypeScript port.** A shorthand `baseURL` or options spread in after Token Factory's settings overrode them, two functions each with their own `const openai` were taken for one call choosing between two clients, and edits came out space-indented in tab-indented files.
- **TypeScript functions their module doesn't export couldn't be tested.** The Node runner now exports one as its module loads, after tsx or Node's type stripping has compiled it: an ES module gets `export { name };`, a CommonJS one `module.exports.name = name;` inside tsx's wrapper. The file is never written.
- **`yarn.lock` and `bun.lock` could only be read**, and 5 of the 20 apps lock with Yarn or bun. Both are upgraded now, by the repo's own Yarn (1, or 2+ through corepack) or bun, with their own age gates where the version has one, and tested with them.
- **Too many calls stayed with only "couldn't find where its client is created"** (29 of them). The port now moves a client wrapped on the way (`instrument(OpenAI())`), follows `client.with_options(...)` back to its client, and finds a `self._client` from a function inside a method or a parent class in the same file. For the rest it says why: the client is another class, it's handed in when the object is built, or a class from outside the app sets it up. 4 are left without a precise reason.
- **A monorepo's subprojects could only be read, not upgraded.** `openport upgrade --project <folder>` (and `upgrade-action --project`) upgrades one, with its own tests, in a copy of the whole repo.

The third round (Oct 25), from Token Factory's own model list:

- **Calls that send images stayed where they were, though a Nemotron now reads them.** Nemotron 3.5 Super VL arrived on Token Factory on Oct 12. A call that sends images now moves to it: its tuning ladder is that one model, since a text tier can't see the image, and the research step reads the VL model's own pages rather than its text sibling's. In the study that moved 1 call of 11, because the other 10 are held back by their client or URL too; in probly, one of the demo repos, it makes 4 of 4.
- **A client handed in was never followed to where it's built.** 29 calls stayed because a caller passed them their client, or their object was built with one. The port now follows the argument back through the app's own calls (up to three, its tests' mocks left out) and moves the call when every caller hands it a client the port can rewrite. 5 moved (3 of headroom's, 2 of learn-claude-code's), and 2 now stay with the precise reason: their callers' clients come from a factory that picks the provider. A function also handed around as a callback, or a method also called on an object from elsewhere, stays: a call the port can't see might pass another client.
- **A call that stayed could have its client swapped by one that moved.** learn-claude-code's context compactor stayed (its client arrived through `__init__`) while the agent's own calls moved, and they share one client, which the port swapped: the compactor would have sent its Claude model name to Token Factory. A client any staying call is handed, as far as the port can trace, is now split or left alone, never swapped under it.

## What it says to build next

- **Building classes that take arguments** (20 calls), from what the app's own tests build them with.
- **Reading the settings that plain HTTP calls take their URL from** (42 calls), where a project keeps them in its own files.
- **Following handed-in clients in TypeScript** (3 calls), as the Python port now does.

## Reproduce

The study script prints this page's totals and per-app table from its results (the stars are recorded as it runs,
so they'll differ from the Oct 24 snapshot above).

```bash
uv run python scripts/field_study.py --advisories     # the 20 apps, about five minutes, then the report
uv run python scripts/field_study.py --summary        # the report again, from the saved results in runs/field-study/
uv run python scripts/field_study.py --advisories NousResearch/hermes-agent lobehub/lobehub   # any repos
uv run openport scan NousResearch/hermes-agent        # one repo's calls, as a table
uv run openport upgrade NousResearch/hermes-agent     # its vulnerable dependencies (a dry run: nothing is run)
uv run openport port shareAI-lab/learn-claude-code    # a ported copy, its diff, and why each call that stays, stays
```
