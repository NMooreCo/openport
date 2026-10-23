# Feedback log: Nebius Token Factory, NVIDIA Nemotron and tools

Running notes for the hackathon's feedback section. Each entry says what happened, why it matters, and what would fix it. Add to it whenever something is confusing, slow or broken.

## Token Factory docs

**Oct 23: The structured-output examples are broken.**
On the "Structured output & JSON" page, the Python `json_schema` example is missing a comma, so it doesn't run. The cURL example sends `"response format"` (with a space) instead of `response_format`, so the JSON mode silently isn't applied. These are the first examples a developer copies when moving structured-output code over.
*Fix:* correct both snippets, and run the examples in CI.

**Oct 23: Nothing says which models support tools or JSON mode.**
The function-calling page shows only a Llama 3.1 example. The JSON page says to "look for the JSON mode tag on the model card" and "test several models". For anyone porting an app, "which Nemotron models do tool calling and `json_schema`?" is the first question.
*Fix:* a capability matrix per model (tools, `tool_choice`, `json_object`, `json_schema`, vision, reasoning toggle). Also return capabilities in `GET /v1/models?verbose=true`, next to pricing, so tools can check them automatically.

**Oct 23: The model-list examples only show Llama 3.1.**
The "List of models" examples (including the verbose one with pricing) only show `meta-llama/Meta-Llama-3.1-*` models. Showing a Nemotron entry would help people find the exact model IDs.

**Oct 23: The "switch from OpenAI" guide oversells how simple it is.**
It says only the base URL, key and model change, with "no other changes". But its own before/after snippets switch from `client.responses.create` (OpenAI Responses API) to `client.chat.completions.create`, without saying whether the Responses API is supported. It also doesn't mention:
- structured outputs
- tool-calling differences
- reasoning output
- that there's no Anthropic-compatible endpoint, so code using the Anthropic SDK needs a full translation

*Fix:* a migration checklist covering these, and an Anthropic section. (This gap is the reason OpenPort exists.)

**Oct 23: The Nemotron 3 reasoning switch isn't documented on Token Factory.**
Nemotron 3 reasons by default. Turning it off (`extra_body={"chat_template_kwargs": {"enable_thinking": false}}`) is documented on Hugging Face model cards and third-party hosts, but I couldn't find it in the Token Factory docs. It matters for latency, cost, and JSON-only outputs.
*Fix:* a per-model "reasoning" section: how to toggle it, where reasoning text comes back in the response, and whether reasoning tokens count against `max_tokens`.

## Smoke test findings

_Filled in after the first `openport smoke` run._

## Nemotron model behaviour

_Filled in after the first `openport baseline` run._
