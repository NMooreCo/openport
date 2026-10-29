# Feedback: building OpenPort on Nebius Token Factory + NVIDIA Nemotron

A running log of friction and wins while building OpenPort, for the Nebius x NVIDIA Global AI Hackathon's feedback prize. Each item is **Expected → Found → Suggestion**.

Two kinds of notes are tracked separately and honestly:
- **[build]** — observations from reading the docs and writing code against the API (true as of the dates shown).
- **[live]** — observations from actually running against Token Factory (from Oct 24). I only report numbers I measured.

## The short version

What would move the most real code onto Token Factory, ranked. The counts come from OpenPort's [field study](docs/FIELD-STUDY.md) of the 20 most-starred open-source AI apps on GitHub, whose own code makes 207 LLM calls in 14 of them; the rest comes from my live runs. Each ask links to its details below.

What already works: 119 of the 207 calls (57%) are plain Chat Completions, which Token Factory's OpenAI compatibility serves as promised once the URL, key and model change. The 51 that use tool calling (25%), the commonest structured shape, need nothing more: Nemotron's OpenAI-format tool calling passed on all three tiers.

1. **An Anthropic-compatible Messages endpoint.** 70 of the 207 calls (34%), in half the apps that make calls (7 of 14), are written for Anthropic's Messages API: 60 through its SDK, 10 over plain HTTP (49 of the 70 are in one app, learn-claude-code). None can move with a base-URL swap. DeepSeek and Moonshot serve such an endpoint for their own models, both of which Token Factory hosts. [Details](#anthropic-sdk-code-has-no-drop-in-path)
2. **One line at the top of the switch guide: raise `max_tokens`, or turn reasoning off.** Reasoning tokens count against `max_tokens`, so a call sized for a closed model's direct answer comes back cut off. It broke preflight's review call, and the obvious fix (reasoning off) cost 17 points of accuracy. [Details](#reasoning-tokens-count-against-max_tokens)
3. **`supported_features` that says what works.** For every Nemotron model it lists only `tools` and `reasoning`, yet JSON mode passed on all three tiers in my live tests, and strict JSON schemas on Super and Ultra. A router that trusts the field would never send the 7 JSON-mode and structured-output calls to Nemotron. [Details](#supported_features-leaves-out-json-support-that-works)
4. **Say plainly that the Responses API works, and on which models.** 10 calls in 4 apps use it (`responses.create`, `responses.parse`, or `/v1/responses` over HTTP). It does work: on Oct 28 `responses.create` answered on all five Nemotron models. But the switch guide's own example turns `responses.create` into `chat.completions.create` while saying no other changes are needed, so it reads as if a rewrite were needed (I thought so on Oct 23). [Details](#the-openai-responses-api-also-needs-rewriting)
5. **Catalog fields tools can rely on:** a family or size, and a real `created` date. Nemotron 3.5 Lightning has Nano's size and price but not its name, so OpenPort's tier detection never tried it, and `created` is the time of the request for 9 of the 30 models. [Details](#the-catalog-doesnt-say-which-models-are-alike-or-when-one-arrived)
6. **A higher request limit for Nano.** The tier evals pick for high-volume calls allows 100 requests a minute, a third of Super's. [Details](#nano-the-tier-for-high-volume-calls-has-the-lowest-request-limit)
7. **A TypeScript framework integration.** 2 of the 20 apps reach models through the Vercel AI SDK, and 56 of the 207 calls are TypeScript, but the integrations page covers Python frameworks only. [Details](#the-integrations-cover-python-frameworks-not-typescript-ones)
8. **Fine-tuning:** a Nemotron base model, a `queued` status, and adapter downloads at zero balance. [Details](#fine-tuning-sft--lora)

---

## Token Factory API (OpenAI compatibility)

<a id="anthropic-sdk-code-has-no-drop-in-path"></a>
**[build] Anthropic SDK code has no drop-in path.** (Oct 23)
- Expected: a provider swap like the OpenAI one — change the base URL and go.
- Found: the switch guide's promise of "no other changes" holds for OpenAI-style chat calls, but there is no Anthropic-compatible endpoint, so apps on the Anthropic SDK (`client.messages.create`) need a real translation of system prompts, content blocks, tools and `tool_use`/`tool_result`. This is the single biggest porting cost, and OpenPort exists partly because of it.
- How common it is (field study, Oct 25): 70 of the 207 calls in the 20 most-starred open-source AI apps speak Anthropic's Messages API, in 7 of the 14 apps that make calls: 60 through the SDK and 10 over plain HTTP. 49 of them are in one app, learn-claude-code; the other six apps hold 21. All four of preflight's calls are Claude Opus.
- Others do it: DeepSeek ([`api.deepseek.com/anthropic`](https://api-docs.deepseek.com/guides/anthropic_api)) and Moonshot (`api.moonshot.ai/anthropic`) serve Anthropic-format endpoints for their own models, mainly so Claude Code runs on them. Token Factory hosts both families (DeepSeek V4, Kimi K2.6 and K3), and its list of coding-assistant integrations has no Claude Code.
- Suggestion: either an Anthropic-compatible shim endpoint, or a short, prominent "porting from Anthropic" page that spells out the message-shape mapping.

<a id="the-openai-responses-api-also-needs-rewriting"></a>
**[build] The docs disagree on whether the OpenAI Responses API works.** (Oct 23, corrected Oct 25)
- Expected: `client.responses.create(...)` to work against the OpenAI-compatible surface.
- What I wrote on Oct 23: only Chat Completions is compatible, so Responses-API calls must be rewritten to `chat.completions`. The [switch guide](https://docs.tokenfactory.nebius.com/switch) reads that way: its "before" example calls `client.responses.create(...)`, its Token Factory example calls `client.chat.completions.create(...)`, and it says "no other changes are required".
- Correction (Oct 25): the API reference documents [`POST /v1/responses`](https://docs.tokenfactory.nebius.com/api-reference/inference/create-a-response) (OpenAPI spec version 20261022), with a Llama 3.3 example. Checked Oct 28: in `openport smoke`, `responses.create` answered on all five Nemotron models (Nano, Super, Ultra, Lightning and Super VL), so Responses-API calls move as written. Only the switch guide still reads otherwise.
- How common it is (field study, Oct 25): 10 of the 207 calls, in 4 of the 20 apps (AutoGPT and lobehub 4 each, hermes-agent and oh-my-openagent 1 each): 7 `responses.create`, 2 `responses.parse` and 1 plain HTTP post to `/v1/responses`. OpenAI recommends this API for new projects, so expect more.
- Suggestion: make the switch guide's two examples the same call, and say which models `/v1/responses` serves and which of its features (built-in tools, `previous_response_id`, background mode) work.

<a id="supported_features-leaves-out-json-support-that-works"></a>
**[live] `supported_features` leaves out JSON support that works.** (Oct 25)
- Expected: `/v1/models?verbose=true`'s `supported_features` to say what each model can do, as it does for DeepSeek V4, gpt-oss-120b and the Qwen3 Instruct models (`json_mode`, `structured_outputs`).
- Found: all four Nemotron text models and Nemotron 3.5 Super VL list only `tools` and `reasoning`. In `openport smoke` (Oct 24), JSON mode passed on Nano, Super and Ultra, and strict `json_schema` output passed on Super and Ultra (Nano failed it). Super VL's `supported_sampling_parameters` is also empty, where every other Nemotron lists temperature, top_p and four more.
- Why it matters: a router or gateway that reads the field would keep JSON calls off Nemotron. In the field study that's 7 calls (4 JSON mode, 3 strict schemas), and every call that parses JSON out of plain text (29 more) is a candidate.
- Suggestion: list `json_mode` for all three tiers and `structured_outputs` for Super and Ultra (and fix Nano's, or say it's unsupported).

<a id="the-integrations-cover-python-frameworks-not-typescript-ones"></a>
**[build] The integrations cover Python frameworks, not TypeScript ones.** (Oct 25)
- Found: of the 20 apps, 3 reach models through LiteLLM, 4 through LangChain and 2 through the Vercel AI SDK. Token Factory's [integrations page](https://docs.tokenfactory.nebius.com/integrations/overview) covers LiteLLM and LangChain (Python) well, but no TypeScript framework, and I found no Vercel AI SDK provider for Nebius. 56 of the 207 calls are TypeScript.
- Suggestion: a Vercel AI SDK page (its OpenAI-compatible provider pointed at Token Factory, with the Nemotron model IDs and the reasoning switch), or a first-party provider package.

**[live] Nano fails strict structured output (`json_schema`); Super and Ultra pass.** (Oct 24)
- Ran `openport smoke` against all three tiers. `json_object` (JSON mode), `auto`/forced tool calling, a tool round-trip, and the reasoning on/off switch all pass on Nano, Super and Ultra. But strict JSON-schema structured output (`chat.completions.parse` with a Pydantic schema) **fails on Nano** while passing on Super and Ultra.
- Impact: a cheap high-volume extraction call that relies on strict schemas can't just drop onto Nano; it needs Super, or a prompt that coaxes schema-valid output. This is exactly what OpenPort's tuning ladder is for, but it's worth a docs note on which tiers support strict structured output.
- Rechecked Oct 28: Nano still fails, now by running through its whole 4,096-token budget without closing the JSON (`LengthFinishReasonError`, 25 s). Super, Ultra, Lightning and Super VL all return schema-valid output in 1 to 10 s. (My own check had also graded the ticket's priority, a judgment the prompt gave no rules for; it now grades only schema adherence and the extracted fields.)
- Confirmed in tuning: for SupportDesk's PII extraction call, adding prompt instructions on Nano made it *worse* (43% on tuning cases, 0% held-out); moving to Super fixed it (100% / 100%).
- Update (Oct 28): with the check grading only the schema and the extracted fields, the recorded run's PII call passed on Nano as written (95% of its cases, 100% held out), so it stays on Nano.

**[live] The reasoning switch is the most powerful tuning knob.** (Oct 24)
- In `openport tune`, 4 of 5 calls ended up changing Nemotron's reasoning setting. Turning it **off** fixed format-following for classification, extraction and reply drafting (and cut output tokens). Turning it **on** let Nano handle a multi-step policy call that previously ran on Claude Opus — at 100% pass rate, $25.07 → $0.31 per 1,000 calls. (Re-measured Oct 28: still on Nano at 100%, $5.37 → $0.40, with a lower estimate of the original model's cost.)
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

<a id="the-catalog-doesnt-say-which-models-are-alike-or-when-one-arrived"></a>
**[live] The catalog doesn't say which models are alike, or when one arrived.** (Oct 25)
- Found: Nemotron 3.5 Lightning (`nvidia/Nemotron-3_5-Lightning`) is, by its own description, a 30B hybrid MoE with 3B active parameters, the shape of Nemotron 3 Nano, at the same price ($0.06 / $0.24 per 1M tokens), with 4× the context (1M tokens) and 6× the request limit. Nothing in the list links the two, so OpenPort's tier detection, which looks for "nano", never tried it; I noticed it on Oct 25, two months after it was added. Nemotron 3.5 Super VL, the first Nemotron that reads images, arrived on Oct 12 the same way.
- Found: `created` is the time of the request for 9 of the 30 models, Nemotron 3 Nano among them. Two calls 20 seconds apart returned two different times for each, a few seconds before the response, so a tool can't use it to spot new models or tell their age.
- Suggestion: `family`, `generation` and size fields (or a tier), and a fixed `created` date for every model.

<a id="nano-the-tier-for-high-volume-calls-has-the-lowest-request-limit"></a>
**[live] Nano, the tier for high-volume calls, has the lowest request limit.** (Oct 25)
- Found: per the models list, Nemotron 3 Nano allows 100 requests a minute; Super, Ultra and Super VL allow 300, and Lightning 600.
- Why it matters: Nano is where OpenPort's evals put the cheap, high-volume calls (in the Oct 28 runs, SupportDesk's triage, PII and policy, and preflight's review and PR description). At 100 a minute, an app making more than about 1.7 of those calls a second can't stay on the model its evals chose.
- Suggestion: a Nano limit at least as high as Super's, or a documented way to raise it.

<a id="reasoning-tokens-count-against-max_tokens"></a>
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
- Re-measured Oct 28 over all 118 cases: 62% on Nano, pooled. The 70% the README and video quote is the recorded run's mean of the five calls' own plain-swap rates.
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

**[build] Spreading eval runs across Serverless Jobs; hosting the dashboard on an Endpoint.** (Oct 24)
- Built and unit-tested (`openport tune --jobs`, `scripts/deploy_endpoint.sh`), but not run: both need AI Cloud credit, which the hackathon doesn't provide, so there's nothing measured to report. The hosted dashboard runs on Cloudflare and calls Token Factory.

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
- Update (Oct 28): once the account had funds again, the adapter downloaded (84 MB of LoRA weights).

---

_Maintained as the build progresses. Dates are when an observation was first recorded._
