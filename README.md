# OpenPort

[![tests](https://github.com/NMooreCo/openport/actions/workflows/tests.yml/badge.svg)](https://github.com/NMooreCo/openport/actions/workflows/tests.yml)

**Prove it, then port it.** OpenPort moves your app from closed LLM APIs (OpenAI, Anthropic) onto NVIDIA Nemotron on Nebius Token Factory, and proves it still works:

1. It finds every LLM call in your repo.
2. It builds an eval for each one.
3. It rewrites the code for Token Factory.
4. It tunes each prompt until Nemotron matches, picking the cheapest model that passes (Nano, Super or Ultra).
5. It opens a PR with the evidence.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) (Coding and Agentic Engineering track).

> **Status:** in progress. The full plan is in [PLAN.md](PLAN.md), and build notes on Token Factory and Nemotron are in [FEEDBACK.md](FEEDBACK.md).

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then add NEBIUS_API_KEY (and TAVILY_API_KEY)
```

Find every OpenAI and Anthropic call in a repo, local or on GitHub, and what each one needs to run on Token Factory. No API keys needed; the code is parsed, never run.

```bash
uv run openport scan examples/supportdesk
uv run openport scan jsliapark/preflight
```

Port a copy of the repo onto Token Factory. Each SDK client is swapped for the OpenPort runtime, and each call gets a Nemotron model. Plain HTTP calls to OpenAI-style endpoints move too, where the code spells out where they go: their URL (or base URL), the environment variable their key comes from, and their model are rewritten. A call that sends images moves to Nemotron 3.5 Super VL, the Nemotron that reads them. Calls that can't move safely stay where they are, with the reason: audio, the provider's own web search, or a URL that comes from settings. The original is never modified. The ported copy lands in `runs/ports/<name>`, the diff in `runs/port-<name>.diff`.

```bash
uv run openport port jsliapark/preflight
uv run openport port examples/supportdesk --assign triage.triage_ticket=nano --assign policy.review_case=ultra
```

Check what Token Factory offers and how each Nemotron model handles JSON, tools and reasoning:

```bash
uv run openport models
uv run openport smoke
```

Measure the "plain swap" baseline for the SupportDesk demo app: what happens if you only change the base URL and model name.

```bash
uv run openport baseline examples/supportdesk
```

For a repo that has no eval spec yet, draft one. `openport capture` scans the repo and writes a starter `openport.evals.yaml` with a call site, a grader and a dataset per LLM call. The grader is chosen from the output shape (field checks for JSON and tool calls, a Nemotron Ultra judge for free text). Point it at recorded calls to fill the datasets, or let Super invent inputs and label them — with Nemotron Ultra by default, or with `--label-with original` to run the app through its original closed model (needs that provider's key). Generated cases are flagged for review, and calls the harness can't run (a method whose class needs arguments to build, a TypeScript function that isn't exported) are listed in `CAPTURE_NOTES.md`, with what it would take. Where the client comes from doesn't matter: while a test runs, the OpenAI and Anthropic SDKs' own calls, sync or async, are routed to Nemotron, and so are plain HTTP requests to LLM endpoints. A call in a class's method runs on an instance built for each case; `init:` in the spec gives the arguments, when building one takes some. The result is runnable by `openport tune`.

```bash
uv run openport capture examples/supportdesk --logs triage.triage_ticket=logs/triage.jsonl
uv run openport capture jsliapark/preflight --generate 10   # needs NEBIUS_API_KEY
```

Tune each call. OpenPort starts it on Nemotron Nano exactly as written. If it misses the target, Nemotron Ultra reads the failures and proposes a fix (extra prompt instructions, reasoning on or off, a higher token limit, temperature), and it tries again, moving up to Super and then Ultra if needed. About 30% of each call's cases are held out: Ultra never sees them, and a fix only counts if it passes those too. The result is a plan for `openport port`. A call that sends images is tuned on Nemotron 3.5 Super VL alone, since a text tier can't see them, and `--tiers` can add Nemotron 3.5 Lightning, which has Nano's size and price (`--tiers nano,lightning,super,ultra`).

```bash
uv run openport tune examples/supportdesk
uv run openport port examples/supportdesk --plan runs/plan-supportdesk.json
```

TypeScript and JavaScript calls on the OpenAI or Anthropic SDK are tested the same way. The app runs unchanged under Node (TypeScript through its own `tsx`), and OpenPort points its SDK at a local stand-in for the API that applies each tuning variant and forwards the request to Token Factory (Anthropic's requests translated to Nemotron chat completions), so the app's process never holds a Token Factory key. `examples/supportdesk-ts` is SupportDesk's three OpenAI calls in TypeScript, tested on the Python app's labelled cases. The port writes the tuned changes into TypeScript too, including the reasoning switch; an Anthropic client becomes OpenPort's `AnthropicOnNemotron`, from a small runtime file it copies in.

```bash
cd examples/supportdesk-ts && npm install && cd ../..
uv run openport tune examples/supportdesk-ts
uv run openport port examples/supportdesk-ts --plan runs/plan-supportdesk-ts.json
```

Calls that send images are tested the same way, on Nemotron 3.5 Super VL. `examples/receipts` is a receipt reader on gpt-4o-mini's vision: it reads the order number, date and total off a photo of a store receipt, and its 24 receipts are drawn from numbers chosen in advance, so every label is right by construction, with the traps real receipts have (subtotals, discounts, cash tendered and change).

```bash
uv run openport tune examples/receipts
```

With `TAVILY_API_KEY` set, before proposing a fix the tuner looks up NVIDIA's own guidance for the exact model it's tuning on (Nemotron 3 Nano, Super or Ultra, not a sibling like Nano Omni). Ultra cites the pages it relied on, and the PR links them next to the change.

Tune every call at once on **Nebius Serverless Jobs**: each call site runs in its own container job, built from this repo's Dockerfile, and the results merge into one report and plan. API keys reach the jobs only as MysteryBox secrets. It needs the `nebius` CLI (Linux or macOS; on Windows, set `OPENPORT_NEBIUS_CLI` to run it through WSL).

```bash
uv run openport tune examples/supportdesk --jobs --job-image <registry>/openport:<tag> --job-platform <cpu-platform> --job-preset <preset> \
  --job-secret NEBIUS_API_KEY=<mysterybox-secret> --job-secret TAVILY_API_KEY=<mysterybox-secret>
```

For a high-volume call, Ultra can teach a smaller model. `openport distill` has Super invent new inputs (never the eval cases), has Ultra answer them with the call's tuned settings, and keeps only the answers that pass the call's own checks, so the training set contains no closed-model outputs. `openport finetune` uploads it to Token Factory and LoRA-fine-tunes an open model on it. Token Factory doesn't fine-tune Nemotron yet, so the default student is Llama 3.1 8B Instruct. Once the student is served (a Dedicated Endpoint, or your own vLLM server), `--model` puts it on the tuning ladder, where it faces the same evals as every tier.

```bash
uv run openport distill examples/supportdesk --site replies --examples 300
uv run openport finetune runs/distill-supportdesk-replies.jsonl --suffix supportdesk-replies
uv run openport tune examples/supportdesk --sites replies --tiers nano,student,super --model student=<model-id>@<base-url>
```

The port writes the tuned changes into the code: prompt additions go into the string or prompt file where each prompt is written, and the reasoning switch, `max_tokens` and `temperature` go into the call. Anything it can't place safely is listed for review.

Write the PR description: each call's model before and after, its score as a plain swap and after tuning, its held-out score, and the estimated cost per 1,000 calls. Closed-model prices come from the providers' pricing pages via Tavily, and a price is used only if the page actually shows it. With `--open`, OpenPort commits the port to a branch, pushes it to your fork and opens the PR there, so the original maintainers aren't notified.

```bash
uv run openport pr runs/ports/jsliapark__preflight          # dry run: writes runs/pr-*.md
uv run openport pr runs/ports/jsliapark__preflight --open   # opens the PR in your fork
```

Keep it honest after merging. `openport port` adds a GitHub Action (when the repo has an eval spec) that runs this on every pull request and fails if any call drops below the bar recorded in `openport.yaml`. It needs a `NEBIUS_API_KEY` repository secret.

```bash
uv run openport check .
```

## Upgrade vulnerable dependencies

The same "prove it, then ship it" idea, pointed at a second kind of PR: patch vulnerable packages and fix the code the upgrade breaks.

1. `openport upgrade` reads every lockfile in the repo, its subprojects' included (`requirements*.txt`, `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`), and asks [OSV.dev](https://osv.dev) which installed versions are vulnerable.
2. For each package it picks the release that fixes the most advisories while being **at least a week old**, so a brand-new malicious release can't be auto-selected. Advisories with no fix yet are reported, not ignored.
3. With `--apply`, it upgrades a copy of the repo and runs the repo's own tests before and after, in a throwaway Docker container (pip and pytest, uv, Poetry, or the project's npm, pnpm, Yarn or bun, whichever it uses). Pins in `requirements.txt` are edited; `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` (Yarn 1 or 2+) and `bun.lock` are updated by their own package manager in the container, resolving nothing newer than the same age cutoff, so the guard covers every package the bump pulls in. A direct dependency keeps its range's style (`^8.5.1` becomes `^9.0.0`) with the lockfile at exactly the chosen version. When two lockfiles lock the same version (a `bun.lock` beside `package-lock.json`, say), both move, and the tests run with the package manager the project names. Before a new lockfile is kept, every version it adds is checked against the registry's release date as well.
4. Tavily finds each upgraded package's own changelog, and the PR quotes what changed between the two versions: what was removed, renamed or now works differently, word for word.
5. If the upgrade breaks the tests, Nemotron Super reads those releases in full, and the advisory, and fixes the application code (never the tests) until they pass.
6. It writes the diff and a PR description with the advisories, the version chosen and why, what changed upstream, the code changes with their sources, and the test results.

```bash
uv run openport upgrade examples/authkit            # dry run: what's vulnerable, and the safe version
uv run openport upgrade examples/authkit --apply    # upgrade a copy, prove it with the tests
uv run openport upgrade-action path/to/repo         # do it every Monday, as a GitHub Action
```

**Monorepos.** A lockfile in a subproject is upgraded with `--project <folder>`: its package manager and tests run in that folder, in a copy of the whole repo, so path dependencies on its neighbours still resolve, and its PR and branch are its own. The dry run says which folder each vulnerable package is locked in. `upgrade-action --project` writes a weekly workflow per project.

**Every week, on its own.** `openport upgrade-action` adds a GitHub Actions workflow that runs the same upgrade every Monday and opens a PR, or updates last week's, only when the repo's tests prove it. When they can't, the run fails, so someone hears about it. It needs a `NEBIUS_API_KEY` repository secret (and `TAVILY_API_KEY` for the changelog quotes). It hasn't run on GitHub yet: it installs OpenPort from this repo, which goes public at submission.

On the `authkit` example (PyJWT 1.7.1, requests 2.31.0): 15 advisories fixed. The tests pass before, 4 of 5 fail after the bump to PyJWT 2.14.0, and Super's two fixes (drop `.decode()`, add `algorithms=["HS256"]`) bring them back to 5 of 5. Its Node twin, `authkit-node`, moves jsonwebtoken 8.5.1 to 9.0.0 (3 advisories) through npm, and its tests pass before and after, so it needs no code change.

## Field study: 20 popular AI apps

OpenPort's scanner, port and upgrade dry run ran over the 20 most-starred open-source AI apps on GitHub, statically: nothing was run. Their own code holds 207 LLM calls in 14 of the apps, 132 through the OpenAI and Anthropic SDKs and 75 over plain HTTP. 160 of them (77%) are testable as written, and the port moves 79 to Nemotron as they are; each of the rest stays with its reason, most often a provider chosen outside the code it edits. They also lock 746 known-vulnerable dependency versions, in 19 of the apps, 92% with a fix at least a week old. The study taught OpenPort to find, test and move plain HTTP calls, to test calls in class methods and in TypeScript functions their module doesn't export, to keep calls Nemotron can't serve (audio, the provider's own web search) and clients whose provider is picked elsewhere, to send images to Nemotron 3.5 Super VL, to follow a client its callers hand in back to where it's built, to scan large repos about 40 times faster, and to read and upgrade monorepos. [Per-app numbers and what it says to build next](docs/FIELD-STUDY.md).

## Dashboard

Watch a whole run live. A port shows the calls found, each tuning attempt, the code changes and the PR description. An upgrade shows the advisories, the version chosen for each package, the tests before and after, each of Super's fixes with its sources, and the PR.

```bash
cd dashboard && npm install && npm run build && cd ..
uv run openport serve            # http://127.0.0.1:8000
```

For hosting, the dashboard only accepts its demo presets and public GitHub repos. It runs one pipeline at a time, reuses a recent result for the same repo, and never opens PRs (it shows the dry run). Upgrades run the repo's tests in Docker; on a server without Docker they stop after choosing safe versions.

To host it on a **Nebius Serverless Endpoint**, `scripts/deploy_endpoint.sh` builds the image, pushes it to a Nebius container registry and starts the endpoint, with the API keys taken from a MysteryBox secret (never baked into the image). It prints the dashboard's URL.

A public dashboard shouldn't run up the key's bill, so the demos replay recorded runs (`examples/recorded`, labelled as recorded and dated) and "Run it live" starts a fresh one. Live runs stop for the day once they've spent `OPENPORT_DAILY_BUDGET_USD` (2 in the image).

Testing a call imports the repo's code and runs it inside the server, next to the API keys. So the Docker image sets `OPENPORT_RUN_REPO_CODE=vetted`: a public deployment runs code only for its bundled demo apps and for OpenPort's eval packs, which pin the exact commit that was reviewed (preflight's is `7a00fa2`). Any other repo is scanned and ported untuned, and the launcher says so up front. Locally, OpenPort tests whatever repo you point it at.

## Repo layout

| Path | What it is |
|---|---|
| `src/openport/` | The OpenPort agent and CLI |
| `src/openport/runtime/openport_llm.py` | A standalone module copied into ported apps. It points OpenAI SDK code at Token Factory and runs Anthropic SDK code on Nemotron. |
| `src/openport/runtime/openport_llm.mjs` | The same for TypeScript and JavaScript: runs Anthropic SDK code on Nemotron with nothing but `fetch` |
| `src/openport/server/` | Dashboard API: runs the pipeline and streams its progress as server-sent events |
| `dashboard/` | Dashboard frontend (Vite, React, Tailwind); builds into `src/openport/server/static/` |
| `examples/supportdesk/` | Demo target: a small support-desk app with five LLM calls on the OpenAI and Anthropic SDKs, plus hand-labelled eval sets |
| `examples/supportdesk-ts/` | SupportDesk's three OpenAI calls in TypeScript (JSON mode, JSON schema, a tool-calling agent), tested on the same cases |
| `examples/receipts/` | A call that sends images: gpt-4o-mini reads photos of store receipts (order number, date, total), with 24 receipt images whose labels are right by construction (`tools/make_receipts.py` draws them) |
| `examples/evalpacks/preflight/` | OpenPort's eval pack for [jsliapark/preflight](https://github.com/jsliapark/preflight), an AI PR reviewer that ships no evals: 119 hand-labelled cases, pinned to a reviewed commit |
| `examples/authkit/` | Upgrade demo: a small app pinned to vulnerable PyJWT and requests releases, with tests |
| `examples/authkit-node/` | The same in Node: jsonwebtoken 8.5.1 locked in `package-lock.json`, with `node --test` tests |
| `scripts/` | `deploy_endpoint.sh` (the dashboard on a Nebius Serverless Endpoint), `field_study.py` (the [field study](docs/FIELD-STUDY.md)) and `monday.py` (the re-measure and recordings that wait on Token Factory credit, as one checked sequence) |
| `tests/` | Offline tests (no API keys needed) |

## How OpenPort uses NVIDIA Nemotron and Nebius

**Token Factory is the inference backend.** OpenPort talks to it with the OpenAI SDK pointed at `https://api.tokenfactory.nebius.com/v1/` and a `NEBIUS_API_KEY`. It resolves the three Nemotron tiers — **Nano**, **Super** and **Ultra** — from the live `/v1/models` catalog at runtime (or from `OPENPORT_MODEL_<TIER>` if you pin them), and reads per-token prices from that same catalog so the cost numbers in every report are the real ones, not estimates.

**The runtime shim makes the swap honest.** OpenAI SDK calls move to Token Factory with just a base URL and model change, and so do plain HTTP calls to OpenAI-style endpoints: the port rewrites their URL, key variable and model where the code spells them out. Anthropic SDK calls can't — there is no Anthropic-compatible API — so [`openport_llm.py`](src/openport/runtime/openport_llm.py) translates Anthropic Messages calls (system parameter, content blocks, tools, `tool_use`/`tool_result`) into Nemotron chat completions at the call boundary, leaving the app's own code untouched. [`openport_llm.mjs`](src/openport/runtime/openport_llm.mjs) does the same in TypeScript and JavaScript, and a test checks the two translate alike.

**OpenPort's own agent uses all three tiers, each for the job it fits** — the same "Ultra for reasoning, Nano/Super for the fast calls" split it recommends to its users:

| Job inside OpenPort | Tier | Where |
|---|---|---|
| Reading closed-model prices off providers' pricing pages | Nano | [`pricing.py`](src/openport/pricing.py) |
| Generating realistic eval inputs when a repo has no logs; default tier for untuned calls | Super | [`capture/generate.py`](src/openport/capture/generate.py) |
| Rewriting prompts to fix failures (the tuning engineer) | Ultra | [`tune/engineer.py`](src/openport/tune/engineer.py) |
| Grading free-text outputs (the LLM judge) | Ultra | [`evals/runner.py`](src/openport/evals/runner.py) |
| Labelling generated eval cases | Ultra | [`capture/generate.py`](src/openport/capture/generate.py) |
| Fixing the code a dependency upgrade broke | Super | [`upgrade/fix.py`](src/openport/upgrade/fix.py) |
| Inventing training inputs, and answering them as the teacher | Super, Ultra | [`distill.py`](src/openport/distill.py) |

**Tavily is how OpenPort reads what changed since any model was trained:** pricing pages, NVIDIA's model guidance and package changelogs. Every quote it uses is checked word for word against the page Tavily returned.

| Job inside OpenPort | Tavily | Where |
|---|---|---|
| Closed-model prices for the cost estimate (Nano reads the page) | Search | [`pricing.py`](src/openport/pricing.py) |
| NVIDIA's guidance for the exact Nemotron model being tuned | Search | [`tune/research.py`](src/openport/tune/research.py) |
| Each upgraded package's changelog, cut to the releases in between and quoted in the PR | Extract, then Search, then Map | [`upgrade/changelog.py`](src/openport/upgrade/changelog.py) |
| The advisory and migration notes, when an upgrade breaks the tests | Search | [`upgrade/research.py`](src/openport/upgrade/research.py) |

**Each of your app's calls lands on the cheapest tier that passes its eval.** `openport tune` starts every call on Nano as written, and only moves it up to Super or Ultra when the eval won't pass otherwise — so the PR shows, per call, the tier chosen, the quality score, and the cost before and after.

> Built, not yet run live: `tune --jobs` on **Nebius Serverless Jobs** (waiting on my AI Cloud setup) and serving the fine-tuned student, which needs a Dedicated Endpoint. Hosting the dashboard on a **Nebius Serverless Endpoint** is next. The plan is in [PLAN.md](PLAN.md).

## License

[MIT](LICENSE)
