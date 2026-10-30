# OpenPort

[![tests](https://github.com/NMooreCo/openport/actions/workflows/tests.yml/badge.svg)](https://github.com/NMooreCo/openport/actions/workflows/tests.yml)

**Prove it, then port it.** OpenPort moves your app from closed LLM APIs (OpenAI, Anthropic) onto NVIDIA Nemotron on Nebius Token Factory, and proves it still works:

1. It finds every LLM call in your repo.
2. It builds an eval for each one.
3. It rewrites the code for Token Factory.
4. It tunes each prompt until Nemotron matches, picking the cheapest model that passes (Nano, Super or Ultra).
5. It opens a PR with the evidence.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) (Coding and Agentic Engineering track).

**Watch:** [the 3-minute demo video](https://youtu.be/IzGuPsC-Bq0).

**Try it:** the hosted dashboard at **https://nebius.narralogy.com** replays the recorded demo runs for anyone, and runs live ones for the judges. The pull requests those runs opened are real, in [narralogy-labs](https://github.com/narralogy-labs) on GitHub.

**Results:** SupportDesk, the demo app, moves all 5 of its calls: a plain swap passes 70% of their cases, OpenPort's tuning 96% (98% held out), at $11.70 → $2.27 per 1,000 runs of each call. [jsliapark/preflight](https://github.com/jsliapark/preflight), an open-source PR reviewer I didn't write, moves all 4 of its Claude Opus calls, 94% cheaper ($22.08 → $1.32). [learn-claude-code](#upgrade-vulnerable-dependencies) (79k stars) gets a security upgrade closing 48 advisories, proven by its own CI.

Build notes on Token Factory and Nemotron, with what I'd improve, are in [FEEDBACK.md](FEEDBACK.md). The build plan and its progress log are in [PLAN.md](PLAN.md).

## For the judges

Where each thing the submission asks for is:

| Asked for | Where |
|---|---|
| Setup instructions | [Quickstart](#quickstart) |
| How OpenPort uses NVIDIA Nemotron | [How OpenPort uses NVIDIA Nemotron](#how-openport-uses-nvidia-nemotron) |
| Where Token Factory accelerated the work | [Where Token Factory accelerated the work](#where-token-factory-accelerated-the-work) |
| Other Nebius tools and services | [Other Nebius tools and services](#other-nebius-tools-and-services) |
| How it uses Tavily (the Best Use of Tavily prize) | [How OpenPort uses Tavily](#how-openport-uses-tavily) |
| Feedback on Token Factory, AI Cloud and the NVIDIA models | [FEEDBACK.md](FEEDBACK.md) |
| An OSI-approved open-source license | [MIT](LICENSE) |

### Testing it

The password for live runs is in the file uploaded with the Devpost submission, which only judges can see.

1. **Watch a recorded run (no password).** Open https://nebius.narralogy.com, click **Open the dashboard** and pick a demo: SupportDesk, jsliapark/preflight or Receipt reader. For an upgrade, click **Upgrade dependencies** and pick shareAI-lab/learn-claude-code or AuthKit. The dashboard replays a real run from Oct 28 or 29, labelled as recorded, and nothing calls Token Factory while it plays. When it ends, **Pull request** shows the PR description, **Code changes** the diff, and **Opened on GitHub** links to the real pull request that run opened.
2. **Run one live.** Enter the password in the **Judges' password** box (on the front page under *Try it*, or in a recorded run's banner) and click **Unlock live runs**. Then open a demo and click **Run it live**: OpenPort tests each call on Nemotron through Token Factory, with Tavily's research, and you watch it climb the tiers. Receipt reader takes about 40 seconds, jsliapark/preflight about 4 minutes, SupportDesk about 10. You can also paste any public GitHub repo: the hosted server only runs code it has vetted (the demos, and preflight at a pinned commit), so another repo is scanned and ported without being tested, and the dashboard says so.
3. **Open a real pull request.** When a live port finishes, click **Open on GitHub**. The PR opens in [narralogy-labs](https://github.com/narralogy-labs): in the demo app's own repo, or in a fork for a repo from GitHub, so its maintainers aren't notified. Clicking again returns the same PR.

Live upgrades on the hosted server stop after choosing the safe versions, because the server can't run Docker for the repo's tests; the recorded upgrades show the whole flow, and `uv run openport upgrade examples/authkit --apply` runs it end to end locally. The hosted demo caps live use at $5 of Token Factory spend and 30 new runs a day; past that, the recorded runs still play.

## Quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and git. Some steps need more: Node.js and npm for TypeScript apps and the dashboard, Docker for `upgrade --apply`, and the [GitHub CLI](https://cli.github.com/) for `pr --open`.

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
uv run openport capture examples/supportdesk --logs triage.triage_ticket=examples/supportdesk/evals/triage.jsonl
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

Keep it honest after merging. `openport port` adds a GitHub Action (when the repo has an eval spec) that runs this on every pull request and fails if any call drops below the bar recorded in `openport.yaml`. It needs a `NEBIUS_API_KEY` repository secret. To run the same check by hand, from the ported repo:

```bash
uv run openport check .
```

## Upgrade vulnerable dependencies

The same "prove it, then ship it" idea, pointed at a second kind of PR: patch vulnerable packages and fix the code the upgrade breaks.

1. `openport upgrade` reads every lockfile in the repo, its subprojects' included (`requirements*.txt`, `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`), and asks [OSV.dev](https://osv.dev) which installed versions are vulnerable.
2. For each package it picks the release that fixes the most advisories while being **at least a week old**, so a brand-new malicious release can't be auto-selected. Advisories with no fix yet are reported, not ignored.
3. With `--apply`, it upgrades a copy of the repo and runs the repo's own tests before and after, in a throwaway Docker container (pip and pytest, uv, Poetry, or the project's npm, pnpm, Yarn or bun, whichever it uses). Pins in `requirements.txt` are edited; `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` (Yarn 1 or 2+) and `bun.lock` are updated by their own package manager in the container, resolving nothing newer than the same age cutoff, so the guard covers every package the bump pulls in. A direct dependency keeps its range's style (`^8.5.1` becomes `^9.0.0`) with the lockfile at exactly the chosen version. When two lockfiles lock the same version (a `bun.lock` beside `package-lock.json`, say), both move, and the tests run with the package manager the project names. Before a new lockfile is kept, every version it adds is checked against the registry's release date as well.
4. Tavily finds each upgraded package's own changelog, and the PR quotes what changed between the two versions: what was removed, renamed or now works differently, word for word.
5. If the upgrade breaks the tests, Nemotron Super reads those releases in full, the advisory, the failing output, the files beside the failing one and every path in the project, and fixes the application code (never the tests), adding a file when the new version requires one. A fix that doesn't pass is undone, and the next round goes to Nemotron Ultra, the same ladder the port climbs.
6. It writes the diff and a PR description with the advisories, the version chosen and why, what changed upstream, the code changes with their sources, and the test results.

```bash
uv run openport upgrade examples/authkit            # dry run: what's vulnerable, and the safe version
uv run openport upgrade examples/authkit --apply    # upgrade a copy, prove it with the tests
uv run openport upgrade-action path/to/repo         # do it every Monday, as a GitHub Action
```

**Monorepos.** A lockfile in a subproject is upgraded with `--project <folder>`: its package manager and tests run in that folder, in a copy of the whole repo, so path dependencies on its neighbours still resolve, and its PR and branch are its own. The dry run says which folder each vulnerable package is locked in. `upgrade-action --project` writes a weekly workflow per project.

**Every week, on its own.** `openport upgrade-action` adds a GitHub Actions workflow that runs the same upgrade every Monday and opens a PR, or updates last week's, only when the repo's tests prove it. When they can't, the run fails, so someone hears about it. It needs a `NEBIUS_API_KEY` repository secret (and `TAVILY_API_KEY` for the changelog quotes). It hasn't run on GitHub yet.

On a real repo, [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) (79k stars, one of the field study's 20), `openport upgrade` moved its Next.js site off 6 vulnerable packages, closing 48 advisories, 38 of them in Next.js (16.1.6 → 16.4.1). The proof is the repo's own CI, run as it runs it: `npm ci`, `tsc --noEmit` and `next build`. It passed before; after the bump the build failed, because Next.js 16.4 won't build a page that has no root layout. Super's first fix didn't pass and was undone; Ultra's added a seven-line root layout that passes its children through, and the CI passed. The recorded run replays on the dashboard, and its PR is [in a fork](https://github.com/narralogy-labs/shareAI-lab-learn-claude-code/pull/1).

On the `authkit` example (PyJWT 1.7.1, requests 2.31.0): 15 advisories fixed. The tests pass before, 4 of 5 fail after the bump to PyJWT 2.14.0, and Super's fix (drop `.decode()`, add `algorithms=["HS256"]`) brings them back to 5 of 5. Its Node twin, `authkit-node`, moves jsonwebtoken 8.5.1 to 9.0.0 (3 advisories) through npm, and its tests pass before and after, so it needs no code change.

## Field study: 20 popular AI apps

OpenPort's scanner, port and upgrade dry run ran over the 20 most-starred open-source AI apps on GitHub, statically: nothing was run. Their own code holds 207 LLM calls in 14 of the apps, 132 through the OpenAI and Anthropic SDKs and 75 over plain HTTP. 160 of them (77%) are testable as written, and the port moves 79 to Nemotron as they are; each of the rest stays with its reason, most often a provider chosen outside the code it edits. They also lock 746 known-vulnerable dependency versions, in 19 of the apps, 92% with a fix at least a week old. The study taught OpenPort to find, test and move plain HTTP calls, to test calls in class methods and in TypeScript functions their module doesn't export, to keep calls Nemotron can't serve (audio, the provider's own web search) and clients whose provider is picked elsewhere, to send images to Nemotron 3.5 Super VL, to follow a client its callers hand in back to where it's built, to scan large repos about 40 times faster, and to read and upgrade monorepos. [Per-app numbers and what it says to build next](docs/FIELD-STUDY.md).

## Dashboard

Watch a whole run live. A port shows the calls found, each tuning attempt, the code changes and the PR description. An upgrade shows the advisories, the version chosen for each package, the tests before and after, each fix (Super's, then Ultra's when Super's fails) with any sources it cites, and the PR.

```bash
cd dashboard && npm install && npm run build && cd ..
uv run openport serve            # http://127.0.0.1:8000
```

For hosting, the dashboard only accepts its demo presets and public GitHub repos. It runs one pipeline at a time and reuses a recent result for the same repo. Upgrades run the repo's tests in Docker; on a server without Docker they stop after choosing safe versions.

A hosted dashboard can't push to your repos, so it opens pull requests in a GitHub organization of its own instead: give it a fine-grained token for that organization (`OPENPORT_GITHUB_TOKEN`, with `OPENPORT_SANDBOX_ORG` naming it), and a finished port gets an **Open on GitHub** button. A repo from GitHub is forked into the organization and the PR opened inside the fork, so its maintainers aren't notified; a demo app gets a repo there whose `main` is the app before the port. The branch is the commit the run started from plus exactly the files the run changed, and the PR body is the run's description, with @mentions and links to other repos' issues defused. The demos' recorded runs link to the PRs opened from them in [`narralogy-labs`](https://github.com/narralogy-labs). To open the PR in your own repo, run `openport pr --open` with your own GitHub login.

The hosted dashboard, at **https://nebius.narralogy.com**, opens on a page that explains OpenPort and plays the recorded demos for anyone; live runs there are for the hackathon's judges, behind a password (see [For the judges](#for-the-judges)). It runs on **Cloudflare**: the same Docker image in a Cloudflare Container behind a Worker, with the API keys as Worker secrets (never baked into the image). [`deploy/cloudflare/`](deploy/cloudflare/README.md) has the config and the steps. The hackathon's rules count Token Factory calls as using the Nebius platform and give no AI Cloud credit, so the dashboard doesn't run on AI Cloud. For anyone who has it, `scripts/deploy_endpoint.sh` puts the same image on a **Nebius Serverless Endpoint**, with the keys taken from a MysteryBox secret.

A **static replay** of the dashboard plays the demos' recorded runs with no server, key or credit behind it: `uv run python scripts/static_replay.py` builds it into `site/`, for any static host (`deploy/cloudflare/replay/` serves it free on Cloudflare). `.github/workflows/pages.yml` publishes it on GitHub Pages, at **https://nmooreco.github.io/openport/**, so the demo keeps working whatever happens to the server or the credit.

A public dashboard shouldn't run up the key's bill, so the demos replay recorded runs (`examples/recorded`, labelled as recorded and dated) and "Run it live" starts a fresh one. Live runs stop for the day once they've spent `OPENPORT_DAILY_BUDGET_USD` (2 in the image; the Cloudflare deployment sets 5).

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
| `deploy/cloudflare/` | The hosted dashboard: a Worker and Container config for the live dashboard, and one for the static replay |
| `scripts/` | `deploy_endpoint.sh` (the dashboard on a Nebius Serverless Endpoint), `field_study.py` (the [field study](docs/FIELD-STUDY.md)) and `monday.py` (the Oct 28 re-measure and recordings, as one checked sequence) |
| `tests/` | Offline tests (no API keys needed) |

## How OpenPort uses NVIDIA Nemotron

Every model OpenPort uses is NVIDIA Nemotron on Nebius Token Factory, and every call it moves lands on one: Nemotron 3 **Nano**, **Super** or **Ultra**, or Nemotron 3.5 **Super VL** for a call that sends images (Nemotron 3.5 Lightning is an optional rung, with Nano's size and price).

**The runtime shim makes the swap honest.** OpenAI SDK calls move to Token Factory with just a base URL and model change, and so do plain HTTP calls to OpenAI-style endpoints: the port rewrites their URL, key variable and model where the code spells them out. Anthropic SDK calls can't — there is no Anthropic-compatible API — so [`openport_llm.py`](src/openport/runtime/openport_llm.py) translates Anthropic Messages calls (system parameter, content blocks, tools, `tool_use`/`tool_result`) into Nemotron chat completions at the call boundary, leaving the app's own code untouched. [`openport_llm.mjs`](src/openport/runtime/openport_llm.mjs) does the same in TypeScript and JavaScript, and a test checks the two translate alike.

**OpenPort's own agent uses all three tiers, each for the job it fits** — the same "Ultra for reasoning, Nano/Super for the fast calls" split it recommends to its users:

| Job inside OpenPort | Tier | Where |
|---|---|---|
| Reading closed-model prices off providers' pricing pages | Nano | [`pricing.py`](src/openport/pricing.py) |
| Generating realistic eval inputs when a repo has no logs; default tier for untuned calls | Super | [`capture/generate.py`](src/openport/capture/generate.py) |
| Rewriting prompts to fix failures (the tuning engineer) | Ultra | [`tune/engineer.py`](src/openport/tune/engineer.py) |
| Grading free-text outputs (the LLM judge) | Ultra | [`evals/runner.py`](src/openport/evals/runner.py) |
| Labelling generated eval cases | Ultra | [`capture/generate.py`](src/openport/capture/generate.py) |
| Fixing the code a dependency upgrade broke (Ultra takes over when Super's fix fails) | Super, then Ultra | [`upgrade/fix.py`](src/openport/upgrade/fix.py) |
| Inventing training inputs, and answering them as the teacher | Super, Ultra | [`distill.py`](src/openport/distill.py) |

**Each of your app's calls lands on the cheapest tier that passes its eval.** `openport tune` starts every call on Nano as written, and only moves it up to Super or Ultra when the eval won't pass otherwise — so the PR shows, per call, the tier chosen, the quality score, and the cost before and after.

## How OpenPort uses Tavily

**Tavily is how OpenPort reads what changed since any model was trained:** pricing pages, NVIDIA's model guidance and package changelogs. Every quote it uses is checked word for word against the page Tavily returned.

| Job inside OpenPort | Tavily | Where |
|---|---|---|
| Closed-model prices for the cost estimate (Nano reads the page) | Search | [`pricing.py`](src/openport/pricing.py) |
| NVIDIA's guidance for the exact Nemotron model being tuned | Search | [`tune/research.py`](src/openport/tune/research.py) |
| Each upgraded package's changelog, cut to the releases in between and quoted in the PR | Extract, then Search, then Map | [`upgrade/changelog.py`](src/openport/upgrade/changelog.py) |
| The advisory and migration notes, when an upgrade breaks the tests | Search | [`upgrade/research.py`](src/openport/upgrade/research.py) |

## Where Token Factory accelerated the work

- **No GPUs and no model servers.** All of the models above, Ultra's 550B parameters included, sit behind one API key. OpenPort talks to them with the OpenAI SDK pointed at `https://api.tokenfactory.nebius.com/v1/`, so there was no inference stack to build or host.
- **Changing tier is changing a string.** The tuner climbs from Nano to Super to Ultra by changing the model name. That's what makes searching every call for the cheapest tier that passes practical at all.
- **Real prices, live.** OpenPort resolves the tiers from the `/v1/models` catalog at runtime (or from `OPENPORT_MODEL_<TIER>` if you pin them) and reads per-token prices from that same catalog, so every cost in every PR is computed from Token Factory's own numbers, not a table kept by hand.
- **Cheap enough to test everything.** A full port of SupportDesk (5 calls, 118 labelled cases, every tuning round, Ultra's rewrites and judging) cost $0.70 of Token Factory. Porting preflight's 4 calls cost about $0.07, and a live port of the receipt reader about $0.02.
- **Fast enough to watch.** On the hosted dashboard, the receipt reader ports in about 40 seconds, preflight in about 4 minutes and SupportDesk in about 10.
- **Fine-tuning without hardware.** `openport distill` and `openport finetune` turn Ultra's checked answers into a LoRA fine-tune through Token Factory's fine-tuning API: a job of 209 examples trained in 14 minutes, with nothing to provision.

## Other Nebius tools and services

- **Token Factory fine-tuning (SFT / LoRA):** used. One distillation job ran and its adapter downloaded. Token Factory doesn't fine-tune Nemotron yet, so the student was Llama 3.1 8B, and serving it needs a Dedicated Endpoint; no result from it is claimed.
- **Nebius Serverless Jobs:** built and unit-tested, not run. `openport tune --jobs` runs each call site in its own container job, with the API keys as **MysteryBox** secrets.
- **Nebius Serverless Endpoint:** built, not run. `scripts/deploy_endpoint.sh` puts the dashboard's image on one, with the keys from a MysteryBox secret.

Serverless Jobs and Endpoints need AI Cloud credit, which the hackathon doesn't provide, so the hosted dashboard runs the same image on Cloudflare and calls Token Factory. The plan is in [PLAN.md](PLAN.md).

## License

[MIT](LICENSE), an OSI-approved open-source license.
