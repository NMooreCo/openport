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

Check what Token Factory offers and how each Nemotron model handles JSON, tools and reasoning:

```bash
uv run openport models
uv run openport smoke
```

Measure the "plain swap" baseline for the SupportDesk demo app: what happens if you only change the base URL and model name.

```bash
uv run openport baseline examples/supportdesk
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
