# OpenPort

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

Port a copy of the repo onto Token Factory. Each SDK client is swapped for the OpenPort runtime, and each call gets a Nemotron model. Calls that can't move safely (images, for example) stay where they are, with the reason. The original is never modified. The ported copy lands in `runs/ports/<name>`, the diff in `runs/port-<name>.diff`.

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

For a repo that has no eval spec yet, draft one. `openport capture` scans the repo and writes a starter `openport.evals.yaml` with a call site, a grader and a dataset per LLM call. The grader is chosen from the output shape (field checks for JSON and tool calls, a Nemotron Ultra judge for free text). Point it at recorded calls to fill the datasets, or let Super invent inputs and Ultra label them. Generated cases are flagged for review, and calls the harness can't run (TypeScript, methods, unusual clients) are listed in `CAPTURE_NOTES.md`. The result is runnable by `openport tune`.

```bash
uv run openport capture examples/supportdesk --logs triage.triage_ticket=logs/triage.jsonl
uv run openport capture jsliapark/preflight --generate 10   # needs NEBIUS_API_KEY
```

Tune each call. OpenPort starts it on Nemotron Nano exactly as written. If it misses the target, Nemotron Ultra reads the failures and proposes a fix (extra prompt instructions, reasoning on or off, a higher token limit, temperature), and it tries again, moving up to Super and then Ultra if needed. About 30% of each call's cases are held out: Ultra never sees them, and a fix only counts if it passes those too. The result is a plan for `openport port`.

```bash
uv run openport tune examples/supportdesk
uv run openport port examples/supportdesk --plan runs/plan-supportdesk.json
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

## Dashboard

Watch a whole run live: calls found, each tuning attempt, the code changes and the PR description.

```bash
cd dashboard && npm install && npm run build && cd ..
uv run openport serve            # http://127.0.0.1:8000
```

For hosting, the dashboard only accepts its demo presets and public GitHub repos. It runs one pipeline at a time, reuses a recent result for the same repo, and never opens PRs (it shows the dry run).

## Repo layout

| Path | What it is |
|---|---|
| `src/openport/` | The OpenPort agent and CLI |
| `src/openport/runtime/openport_llm.py` | A standalone module copied into ported apps. It points OpenAI SDK code at Token Factory and runs Anthropic SDK code on Nemotron. |
| `src/openport/server/` | Dashboard API: runs the pipeline and streams its progress as server-sent events |
| `dashboard/` | Dashboard frontend (Vite, React, Tailwind); builds into `src/openport/server/static/` |
| `examples/supportdesk/` | Demo target: a small support-desk app with five LLM calls on the OpenAI and Anthropic SDKs, plus hand-labelled eval sets |
| `tests/` | Offline tests (no API keys needed) |

## How OpenPort uses NVIDIA Nemotron and Nebius

**Token Factory is the inference backend.** OpenPort talks to it with the OpenAI SDK pointed at `https://api.tokenfactory.nebius.com/v1/` and a `NEBIUS_API_KEY`. It resolves the three Nemotron tiers — **Nano**, **Super** and **Ultra** — from the live `/v1/models` catalog at runtime (or from `OPENPORT_MODEL_<TIER>` if you pin them), and reads per-token prices from that same catalog so the cost numbers in every report are the real ones, not estimates.

**The runtime shim makes the swap honest.** OpenAI SDK calls move to Token Factory with just a base URL and model change. Anthropic SDK calls can't — there is no Anthropic-compatible API — so [`openport_llm.py`](src/openport/runtime/openport_llm.py) translates Anthropic Messages calls (system parameter, content blocks, tools, `tool_use`/`tool_result`) into Nemotron chat completions at the call boundary, leaving the app's own code untouched.

**OpenPort's own agent uses all three tiers, each for the job it fits** — the same "Ultra for reasoning, Nano/Super for the fast calls" split it recommends to its users:

| Job inside OpenPort | Tier | Where |
|---|---|---|
| Reading closed-model prices off providers' pricing pages | Nano | [`pricing.py`](src/openport/pricing.py) |
| Generating realistic eval inputs when a repo has no logs; default tier for untuned calls | Super | [`capture/generate.py`](src/openport/capture/generate.py) |
| Rewriting prompts to fix failures (the tuning engineer) | Ultra | [`tune/engineer.py`](src/openport/tune/engineer.py) |
| Grading free-text outputs (the LLM judge) | Ultra | [`evals/runner.py`](src/openport/evals/runner.py) |
| Labelling generated eval cases | Ultra | [`capture/generate.py`](src/openport/capture/generate.py) |

**Each of your app's calls lands on the cheapest tier that passes its eval.** `openport tune` starts every call on Nano as written, and only moves it up to Super or Ultra when the eval won't pass otherwise — so the PR shows, per call, the tier chosen, the quality score, and the cost before and after.

> Still to come: spreading eval runs across **Nebius Serverless Jobs** for parallelism, hosting the dashboard on a **Nebius Serverless Endpoint**, and **Token Factory fine-tuning** (Ultra teaching Nano on high-volume calls). These are in [PLAN.md](PLAN.md) and need live API keys to exercise.

## License

[MIT](LICENSE)
