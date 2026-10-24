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

**[live] Nano fails strict structured output (`json_schema`); Super and Ultra pass.** (Oct 24)
- Ran `openport smoke` against all three tiers. `json_object` (JSON mode), `auto`/forced tool calling, a tool round-trip, and the reasoning on/off switch all pass on Nano, Super and Ultra. But strict JSON-schema structured output (`chat.completions.parse` with a Pydantic schema) **fails on Nano** while passing on Super and Ultra.
- Impact: a cheap high-volume extraction call that relies on strict schemas can't just drop onto Nano; it needs Super, or a prompt that coaxes schema-valid output. This is exactly what OpenPort's tuning ladder is for, but it's worth a docs note on which tiers support strict structured output.
- Confirmed in tuning: for SupportDesk's PII extraction call, adding prompt instructions on Nano made it *worse* (43% on tuning cases, 0% held-out); moving to Super fixed it (100% / 100%).

**[live] The reasoning switch is the most powerful tuning knob.** (Oct 24)
- In `openport tune`, 4 of 5 calls ended up changing Nemotron's reasoning setting. Turning it **off** fixed format-following for classification, extraction and reply drafting (and cut output tokens). Turning it **on** let Nano handle a multi-step policy call that previously ran on Claude Opus — at 100% pass rate, $25.07 → $0.31 per 1,000 calls.
- Suggestion: a short guide on when to switch reasoning off (format-strict outputs) versus on (multi-step decisions) would help anyone porting from closed models.

**[live] One recommended setting for every task can hurt strict calls.** (Oct 24)
- With Tavily research on, OpenPort's tuner read the Nemotron 3 model cards and adopted their recommended sampling settings in several fixes, citing the cards. That helped a multi-step policy call on Nano (reasoning on: 21/21 cases). But a tool-calling refund agent on Nano fell from 94% of its tuning cases to 4/24 after a cited fix switched reasoning off at temperature 0.6, and the strict-format reply drafter on Super slipped from 13/20 to 11–12/20 at the recommended temperature of 1.0.
- Suggestion: model cards could give settings per kind of task (strict structured or format-bound output, tool calling, open-ended chat) rather than one default for everything.
- Smaller: searches for one model's guidance regularly return pages for its siblings (Nano vs Nano Omni, Super 3 vs Super 3.5 VL), whose settings differ. Clearer per-variant titles would help tools and people find the right page.

## Nemotron models and tiers

**[build] Model IDs must be discovered, not assumed.** (Oct 23)
- Expected: stable, documented model identifiers to hard-code.
- Found: OpenPort resolves the Nano/Super/Ultra tier IDs from `/v1/models` at runtime instead of hard-coding, which is more robust but means a startup round-trip and some guessing about which listed model is which tier.
- Suggestion: a documented tier label (or a `family`/`tier` field in the models list) so tooling can map "the small Nemotron" to an ID without heuristics.

**[live] A plain base-URL swap does leave a real, measurable gap.** (Oct 24)
- First `openport baseline` on SupportDesk (54 cases): a plain swap to Nemotron scores **74%** on Nano, **83%** on Super, **85%** on Ultra — none at parity with the original closed models.
- The free-text reply call (strict tone/format rules) collapses on a plain swap: **0% / 25% / 38%** across the tiers, because Nemotron doesn't follow the app's sign-off and no-promises rules without tuning. The JSON/tool calls fare much better (refunds 100%, policy 89–100%).
- Takeaway: the open models are capable, but "just change the base URL" is not safe to ship as-is — which is the whole reason OpenPort proves each call before porting it.

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
