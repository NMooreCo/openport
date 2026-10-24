# Feedback: building OpenPort on Nebius Token Factory + NVIDIA Nemotron

A running log of friction and wins while building OpenPort, for the Nebius x NVIDIA Global AI Hackathon's feedback prize. Each item is **Expected → Found → Suggestion**.

Two kinds of notes are tracked separately and honestly:
- **[build]** — observations from reading the docs and writing code against the API (true as of the dates shown).
- **[live]** — observations from actually running against Token Factory. These are **pending API keys** and will be filled in as I do real runs; I am not reporting numbers I haven't measured.

---

## Token Factory API (OpenAI compatibility)

**[build] Anthropic SDK code has no drop-in path.** (Oct 23)
- Expected: a provider swap like the OpenAI one — change the base URL and go.
- Found: the switch guide's promise of "no other changes" holds for OpenAI-style chat calls, but there is no Anthropic-compatible endpoint, so apps on the Anthropic SDK (`client.messages.create`) need a real translation of system prompts, content blocks, tools and `tool_use`/`tool_result`. This is the single biggest porting cost, and OpenPort exists partly because of it.
- Suggestion: either an Anthropic-compatible shim endpoint, or a short, prominent "porting from Anthropic" page that spells out the message-shape mapping.

**[build] The OpenAI Responses API also needs rewriting.** (Oct 23)
- Expected: `client.responses.create(...)` to work against the OpenAI-compatible surface.
- Found: only Chat Completions is compatible; Responses-API calls must be rewritten to `chat.completions`.
- Suggestion: call this out explicitly in the compatibility docs — it's easy to assume "OpenAI-compatible" includes the newer Responses API.

**[live] Tool calling and JSON/structured-output reliability per tier.** (pending keys)
- I built `openport smoke` to check `json_object`, `json_schema`, forced/`auto` tool choice and the reasoning switch on Nano, Super and Ultra. Results go here once I can run it.

## Nemotron models and tiers

**[build] Model IDs must be discovered, not assumed.** (Oct 23)
- Expected: stable, documented model identifiers to hard-code.
- Found: OpenPort resolves the Nano/Super/Ultra tier IDs from `/v1/models` at runtime instead of hard-coding, which is more robust but means a startup round-trip and some guessing about which listed model is which tier.
- Suggestion: a documented tier label (or a `family`/`tier` field in the models list) so tooling can map "the small Nemotron" to an ID without heuristics.

**[live] Does a plain base-URL swap actually degrade quality?** (pending keys)
- The whole headline depends on measuring this. Numbers go here after the first `openport baseline` run.

## Pricing

**[build] Live per-token pricing via `/v1/models?verbose=true` is great.** (Oct 23)
- Expected: a separate pricing page or manual table.
- Found: prices come back on the models endpoint, so OpenPort reports real Nebius-side cost rather than estimates. This is a genuine win for a cost-comparison tool.
- Suggestion: keep it; documenting the exact fields and units prominently would save a guess.

## Serverless Jobs / Endpoints

**[live] Spreading eval runs across Serverless Jobs; hosting the dashboard on an Endpoint.** (pending keys)
- Setup friction, cold starts and parallel-run behaviour go here once wired up.

## Fine-tuning (SFT / LoRA)

**[live] "Ultra teaches Nano": fine-tuning a small model on Ultra-generated outputs.** (pending keys)
- Turnaround time, job UX and the quality lift go here after the first tune.

---

_Maintained as the build progresses. Dates are when an observation was first recorded._
