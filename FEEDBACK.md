# Feedback: building OpenPort on Nebius Token Factory + NVIDIA Nemotron

A running log of friction and wins while building OpenPort, for the Nebius x NVIDIA Global AI Hackathon's feedback prize. Each item is **Expected → Found → Suggestion**.

Two kinds of notes are tracked separately and honestly:
- **[build]** — observations from reading the docs and writing code against the API (true as of the dates shown).
- **[live]** — observations from actually running against Token Factory (from Oct 24). I only report numbers I measured.

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

**[live] Reasoning tokens count against `max_tokens`, so apps ported from closed models get cut off.** (Oct 24)
- [preflight](https://github.com/jsliapark/preflight) asks for `max_tokens=1024`, plenty for Claude's direct answer. On Nemotron, reasoning is on by default and its tokens come out of the same budget: on a five-line diff Nano spent 800 of the 1,024 tokens, most of them reasoning. On bigger diffs the JSON answer arrives cut off, and the app reports a parse error.
- That reads like a formatting problem, and the obvious fix (reasoning off) cost accuracy on this review task: 83% → 66% of the cases on Nano. On Super, keeping reasoning on and raising the budget to 4,096 (plus stricter instructions) passed. OpenPort's tuner now tells its engineer when a failing reply hit the token limit.
- Suggestion: say this in the switch guide's first paragraph. "Raise `max_tokens` when you move a call over, or turn reasoning off" is the single most useful line for anyone porting. A separate reasoning budget, if one exists, deserves the same prominence.

**[live] `usage` reports reasoning tokens for Super but not for Nano.** (Oct 24)
- The same request with reasoning on: Super's `usage.completion_tokens_details.reasoning_tokens` says 173 of 178 completion tokens were reasoning; Nano returns `completion_tokens_details: null`, though its reply has a `reasoning_content` too.
- Why it matters: anyone comparing costs with a closed model needs to know how much of the output was reasoning. OpenPort estimates Nano's split from the length of the reasoning text, which is a guess.
- Suggestion: report the reasoning count for every model that reasons.

**[live] Super's code edits are right, but its copied snippets can be mis-indented.** (Oct 24)
- `openport upgrade --apply` has Nemotron Super repair code a dependency upgrade broke, as exact search-and-replace edits. Its diagnosis was right every time (PyJWT 2 returns `str` from `encode`; `decode` needs `algorithms=`), and it cited the PyJWT changelog Tavily found. But in one run it copied the line to replace with 8 spaces of indentation where the file has 4, and repeated the same snippet three rounds in a row, so no edit could apply and the upgrade wasn't proven.
- OpenPort now matches snippets regardless of indentation (only when exactly one place matches) and re-indents the replacement. The same run then proved the upgrade in two rounds.
- Suggestion: tools built on Nemotron's code edits should expect this. A short note in the docs on reliable edit formats (search/replace with indentation-tolerant matching, or unified diffs) would save others the debugging.

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

**[build] The `nebius` CLI doesn't run on Windows, and a job defaults to a GPU.** (Oct 24)
- Expected: one CLI for every OS, and a job that runs where you'd expect a script to.
- Found: the CLI installs on Ubuntu and macOS only, so on Windows OpenPort calls it through WSL (`OPENPORT_NEBIUS_CLI`). And `nebius ai job create` uses a GPU platform (H100) unless you name another, so a CPU-only job, like tuning that only calls Token Factory, bills GPU time by default unless you set a CPU platform. OpenPort refuses GPU platforms for tuning jobs unless asked.
- Suggestion: a Windows build (or a note pointing to WSL), and either a CPU default for jobs or a confirmation when a job lands on a GPU.

**[live] Spreading eval runs across Serverless Jobs; hosting the dashboard on an Endpoint.** (pending AI Cloud setup)
- Setup friction, cold starts and parallel-run behaviour go here once it runs.

## Fine-tuning (SFT / LoRA)

**[build] No Nemotron model can be fine-tuned yet, and serving a fine-tune needs a Dedicated Endpoint.** (Oct 24)
- Expected: "Ultra teaches Nano": fine-tune Nemotron Nano on Ultra's answers and serve it like the other tiers.
- Found: the fine-tunable list has no Nemotron model, so OpenPort's student is Llama 3.1 8B Instruct (LoRA). Meta models are served only through Dedicated Endpoints, and serving custom weights is a beta you request. The fine-tuning page I first found returned 404; the working docs are under `/post-training/`, found via `llms.txt`.
- Suggestion: Nemotron Nano fine-tuning would make "the big model teaches the small one" possible inside one model family. Until then, a single page that walks from the dataset to a served fine-tune would save a lot of digging.

**[live] A fine-tuning job reports "running" with no progress and no queue position.** (Oct 24)
- My first LoRA job (Llama 3.1 8B, 209 training examples, 3 epochs) processed both datasets within a minute, then sat at `status: running` with `trained_steps: 0`, `total_steps: null` and no new events for over an hour.
- Suggestion: a `queued` status, a queue position or an ETA. A job that says "running" while waiting for capacity makes it hard to tell a queue from a stuck job.
- Update: it was a queue. A "Training started" event arrived 2 h 21 min after submission; the status said `running` the whole time.

**[live] The fine-tuning events list returned 500s mid-training.** (Oct 24)
- A few minutes into training, `GET /v1/fine_tuning/jobs/{id}/events` returned `500 {"detail": {"message": "Internal Server Error"}}` through the OpenAI SDK's two retries, and OpenPort's follower stopped. The job carried on, and the same call worked again minutes later.
- What OpenPort does now: it treats 5xx, connection errors and 429s on the job endpoints as transient and retries at the next poll.
- Suggestion: if the events list can fail while a job runs, say so in the docs, or serve the last known events instead of a 500.

**[live] A finished fine-tune's adapter can't be downloaded once the balance hits zero.** (Oct 24)
- The job succeeded (14 minutes of training, 193,041 tokens, checkpoints at steps 2, 4 and 6), and listing its checkpoints works, but fetching their files returns `402 Payment Required: You have exhausted your budget`. The training ran on credit I no longer have, so the weights are stuck until funds are added.
- Suggestion: let an account download what it already paid for, or warn before a job starts that a later download needs a positive balance.

---

_Maintained as the build progresses. Dates are when an observation was first recorded._
