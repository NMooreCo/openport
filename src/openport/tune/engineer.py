"""The prompt engineer: Nemotron Ultra reads a call site's failures and proposes the next variant."""

import json
import re
import time
from typing import Callable

from openai import OpenAI

from .. import tokenfactory
from ..citations import parse_citations, verified
from ..evals.runner import CaseResult
from ..evals.spec import CallSite
from ..runtime.openport_llm import UsageMeter
from .research import Guidance
from .variant import Variant

# engineer(site, model, variant, sample_request, failures, guidance=None, systems=None) -> next variant
Engineer = Callable[..., Variant]

SYSTEM = """You tune LLM calls so an app keeps working after it moves from OpenAI or Anthropic models to NVIDIA Nemotron on Nebius Token Factory.

You can change only these knobs:
- reasoning: true or false turns Nemotron's built-in reasoning on or off (null keeps the model default, which is on). Reasoning helps multi-step decisions but costs tokens and latency; for short classification or extraction it is often unnecessary.
- system_addendum: short instructions appended to the app's system prompt. Use it to fix systematic problems: output format, missing fields, ignored rules, extra chatter around JSON.
- max_tokens: a minimum output budget. Raise it if answers are cut off, especially with reasoning on: reasoning tokens count against the budget, so a reply cut off with reasoning on needs a bigger budget, which keeps the reasoning that may be helping.
- temperature: sampling temperature, or null for the default.

Rules:
- Fix the kind of failure, never a specific case. Do not quote or hint at any expected answer, name, number or label from the failures. Your change is checked on cases you will never see.
- State the general rule rather than listing the examples you saw. If the failures show one kind of input being mishandled, cover the whole kind (for example "every currency", not the two currencies that failed), because the unseen cases will include ones that aren't in the failures.
- Keep system_addendum under 120 words, plain and specific. Don't restate the whole prompt.
- Change as little as you need to. Keep what already works.
- If excerpts from NVIDIA's documentation are included, use them where they explain the failures. Treat general recommendations, like default sampling settings, as background rather than rules: change temperature or reasoning only when the failures point to it.
- Cite an excerpt in sources only if your change follows something it says, with that sentence copied word for word as the quote. Citations are checked against the excerpts, and an empty list is normal.

Respond with JSON only:
{"reasoning": true | false | null, "system_addendum": "...", "max_tokens": integer or null, "temperature": number or null, "rationale": "one sentence on what you're fixing", "sources": [{"url": "...", "quote": "the sentence you followed"}]}"""


def _clip(value, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " [...]"


def _system_prompt(sample: dict | None) -> str:
    for message in (sample or {}).get("messages", []):
        if isinstance(message, dict) and message.get("role") in ("system", "developer"):
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return "(no system prompt)"


def describe_prompts(prompts: list[str]) -> str:
    if len(prompts) == 1:
        return f"The app's system prompt:\n<<<\n{_clip(prompts[0], 4000)}\n>>>"
    # One call can send several prompts, like a review helper that loads a different prompt file per pass.
    shown = "\n".join(f"System prompt {i}:\n<<<\n{_clip(prompt, 3000)}\n>>>" for i, prompt in enumerate(prompts, 1))
    return (
        f"This call runs with {len(prompts)} different system prompts, and system_addendum is appended to every one of them. "
        f"Write it so it's right for each: rules that hold for all of them, never one prompt's task added to another.\n{shown}"
    )


def describe_failures(failures: list[tuple[dict, CaseResult]], limit: int = 8) -> str:
    lines = []
    for case, result in failures[:limit]:
        problems = "; ".join(filter(None, [result.error, *result.failures])) or "scored below the bar"
        lines.append(
            f"- input: {_clip(case['input'])}\n  expected: {_clip(case['expected'], 300)}\n"
            f"  got: {_clip(result.output, 400)}\n  problems: {_clip(problems, 300)}"
        )
    if len(failures) > limit:
        lines.append(f"- ...and {len(failures) - limit} more like these")
    return "\n".join(lines)


def parse_variant(text: str, fallback: Variant) -> Variant:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fallback
    max_tokens = data.get("max_tokens")
    temperature = data.get("temperature")
    return Variant(
        reasoning=data.get("reasoning") if isinstance(data.get("reasoning"), bool) else None,
        system_addendum=str(data.get("system_addendum") or "").strip(),
        max_tokens=int(max_tokens) if isinstance(max_tokens, (int, float)) and max_tokens > 0 else None,
        temperature=float(temperature) if isinstance(temperature, (int, float)) else None,
        rationale=str(data.get("rationale") or "").strip(),
        sources=parse_citations(data.get("sources")),
    )


def cite(variant: Variant, guidance: Guidance | None) -> Variant:
    """Keep only citations whose quote is in an excerpt the engineer was given, with the page's title."""
    variant.sources = verified(variant.sources, guidance.sources if guidance else [])
    return variant


def make_engineer(client: OpenAI | None = None, model: str | None = None, meter: UsageMeter | None = None) -> Engineer:
    client = client or tokenfactory.client()
    model = model or tokenfactory.tiers()["ultra"]

    def engineer(site: CallSite, target_model: str, variant: Variant, sample: dict | None, failures,
                 guidance: Guidance | None = None, systems: list[str] | None = None,
                 history: list[tuple[str, float]] | None = None) -> Variant:
        settings = {k: v for k, v in variant.to_dict().items() if k != "sources"}
        prompt = (
            f"Call site: {site.title} ({site.sdk} SDK, {site.output} output), moving off {site.original_model}.\n"
            f"Running on: {target_model}\n"
            f"Current settings: {json.dumps(settings)}\n"
        )
        if history and len(history) > 1:  # what was tried on this model, so a change that hurt gets undone
            prompt += "Tried so far on this model (share of tuning cases passed):\n" + "\n".join(
                f"- {described}: {rate:.0%}" for described, rate in history
            ) + "\nKeep what scored best and undo changes that made it worse. The failures below are from the latest attempt.\n"
        prompt += f"\n{describe_prompts(systems or [_system_prompt(sample)])}\n\nFailing cases:\n{describe_failures(failures)}"
        if guidance is not None and guidance.sources:
            prompt += "\n\nGuidance from NVIDIA's documentation for this model (found with Tavily):\n" + "\n".join(
                f"- {s['title']} ({s['url']}): {s['excerpt']}" for s in guidance.sources
            )
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        if meter is not None:
            meter.record(model, response.usage, time.perf_counter() - start)
        proposed = parse_variant(response.choices[0].message.content or "", fallback=variant)
        return proposed if proposed is variant else cite(proposed, guidance)

    return engineer
