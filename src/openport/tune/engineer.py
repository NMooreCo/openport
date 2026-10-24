"""The prompt engineer: Nemotron Ultra reads a call site's failures and proposes the next variant."""

import json
import re
import time
from typing import Callable

from openai import OpenAI

from .. import tokenfactory
from ..evals.runner import CaseResult
from ..evals.spec import CallSite
from ..runtime.openport_llm import UsageMeter
from .variant import Variant

# engineer(site, model, variant, sample_request, failures) -> next variant
Engineer = Callable[[CallSite, str, Variant, dict | None, list[tuple[dict, CaseResult]]], Variant]

SYSTEM = """You tune LLM calls so an app keeps working after it moves from OpenAI or Anthropic models to NVIDIA Nemotron on Nebius Token Factory.

You can change only these knobs:
- reasoning: true or false turns Nemotron's built-in reasoning on or off (null keeps the model default, which is on). Reasoning helps multi-step decisions but costs tokens and latency; for short classification or extraction it is often unnecessary.
- system_addendum: short instructions appended to the app's system prompt. Use it to fix systematic problems: output format, missing fields, ignored rules, extra chatter around JSON.
- max_tokens: a minimum output budget. Raise it if answers are cut off, especially with reasoning on.
- temperature: sampling temperature, or null for the default.

Rules:
- Fix the kind of failure, never a specific case. Do not quote or hint at any expected answer, name, number or label from the failures. Your change is checked on cases you will never see.
- Keep system_addendum under 120 words, plain and specific. Don't restate the whole prompt.
- Change as little as you need to. Keep what already works.

Respond with JSON only:
{"reasoning": true | false | null, "system_addendum": "...", "max_tokens": integer or null, "temperature": number or null, "rationale": "one sentence on what you're fixing"}"""


def _clip(value, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " [...]"


def _system_prompt(sample: dict | None) -> str:
    for message in (sample or {}).get("messages", []):
        if isinstance(message, dict) and message.get("role") in ("system", "developer"):
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return "(no system prompt)"


def describe_failures(failures: list[tuple[dict, CaseResult]], limit: int = 8) -> str:
    lines = []
    for case, result in failures[:limit]:
        problems = result.error or "; ".join(result.failures) or "scored below the bar"
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
    )


def make_engineer(client: OpenAI | None = None, model: str | None = None, meter: UsageMeter | None = None) -> Engineer:
    client = client or tokenfactory.client()
    model = model or tokenfactory.tiers()["ultra"]

    def engineer(site: CallSite, target_model: str, variant: Variant, sample: dict | None, failures) -> Variant:
        prompt = (
            f"Call site: {site.title} ({site.sdk} SDK, {site.output} output), moving off {site.original_model}.\n"
            f"Running on: {target_model}\n"
            f"Current settings: {json.dumps(variant.to_dict())}\n\n"
            f"The app's system prompt:\n<<<\n{_clip(_system_prompt(sample), 4000)}\n>>>\n\n"
            f"Failing cases:\n{describe_failures(failures)}"
        )
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        if meter is not None:
            meter.record(model, response.usage, time.perf_counter() - start)
        return parse_variant(response.choices[0].message.content or "", fallback=variant)

    return engineer
