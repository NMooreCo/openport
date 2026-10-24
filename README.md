# OpenPort

**Prove it, then port it.** OpenPort moves your app from closed LLM APIs (OpenAI, Anthropic) onto NVIDIA Nemotron on Nebius Token Factory, and proves it still works:

1. It finds every LLM call in your repo.
2. It builds an eval for each one.
3. It rewrites the code for Token Factory.
4. It tunes each prompt until Nemotron matches, picking the cheapest model that passes (Nano, Super or Ultra).
5. It opens a PR with the evidence.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) (Coding and Agentic Engineering track).

> **Status:** in progress. The full plan is in [PLAN.md](PLAN.md).

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

Tune each call. OpenPort starts it on Nemotron Nano exactly as written. If it misses the target, Nemotron Ultra reads the failures and proposes a fix (extra prompt instructions, reasoning on or off, a higher token limit, temperature), and it tries again, moving up to Super and then Ultra if needed. About 30% of each call's cases are held out: Ultra never sees them, and a fix only counts if it passes those too. The result is a plan for `openport port`.

```bash
uv run openport tune examples/supportdesk
uv run openport port examples/supportdesk --plan runs/plan-supportdesk.json
```

## Repo layout

| Path | What it is |
|---|---|
| `src/openport/` | The OpenPort agent and CLI |
| `src/openport/runtime/openport_llm.py` | A standalone module copied into ported apps. It points OpenAI SDK code at Token Factory and runs Anthropic SDK code on Nemotron. |
| `examples/supportdesk/` | Demo target: a small support-desk app with five LLM calls on the OpenAI and Anthropic SDKs, plus hand-labelled eval sets |
| `tests/` | Offline tests (no API keys needed) |

## How OpenPort uses NVIDIA Nemotron and Nebius

_To be completed as the build progresses._

## License

[MIT](LICENSE)
