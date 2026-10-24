"""Generate a starter eval set when there are no logs: Super invents realistic inputs, Ultra labels them.

This is the fallback path from the plan. Generated cases are a starting point, not ground truth, so
every site that uses them is flagged for human review. Nothing in the target repo is executed: Ultra
produces the reference output from the call's own source, the same material a reviewer would read.
"""

import importlib
import json
import os
import re
import sys
import time
from typing import Callable

from openai import OpenAI
from pydantic import BaseModel

from .. import tokenfactory
from ..runtime.openport_llm import UsageMeter

INPUT_SYSTEM = """You write test inputs for an LLM call so it can be evaluated after moving to a new model.
You are given the function that makes the call and its source. Produce realistic, varied inputs a real
user or caller would send: cover the common case, edge cases, and at least one tricky or adversarial one.
Respond with JSON only: an array of objects, each a complete set of keyword arguments for the function
(keys exactly match its parameters). No prose, no markdown fences."""

LABEL_SYSTEM = """You produce the reference output an LLM call is expected to return, to grade a new model against.
You are given the call's source and one input. Return exactly what a correct implementation should output
for that input, in the same shape the code parses (for JSON outputs, the parsed object; for tool calls, the
chosen action and its arguments). Respond with JSON only, no prose, no markdown fences."""


def _json_block(text: str) -> str | None:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    return match.group(0) if match else None


def input_prompt(title: str, function: str, param_names: list[str], source: str, n: int) -> str:
    params = ", ".join(param_names) if param_names else "(unknown — read them from the source)"
    return (
        f"Call site: {title}\nFunction: {function}({params})\n\n"
        f"Source:\n<<<\n{source[:6000]}\n>>>\n\n"
        f"Write {n} input objects for this function."
    )


def parse_inputs(text: str, param_names: list[str]) -> list[dict]:
    block = _json_block(text)
    if not block:
        return []
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else [data]
    inputs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kept = {k: v for k, v in row.items() if k in param_names} if param_names else row
        if kept:
            inputs.append(kept)
    return inputs


def parse_label(text: str):
    block = _json_block(text)
    if block is None:
        return None
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def original_labeller(root, module_name: str, function_name: str) -> Callable[[dict], object]:
    """A labeller that runs the app's own function through its original model — so it executes the repo.

    Needs the original provider's real key in the environment; placeholders are set only for the other
    provider so the module can import.
    """
    os.environ.setdefault("OPENAI_API_KEY", "openport-placeholder")
    os.environ.setdefault("ANTHROPIC_API_KEY", "openport-placeholder")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)

    def label(case_input: dict) -> object:
        output = function(**case_input)
        return output.model_dump() if isinstance(output, BaseModel) else output

    return label


def generate_cases(
    site_id: str,
    title: str,
    function: str,
    param_names: list[str],
    source: str,
    *,
    n: int,
    needs_label: bool,
    labeller: Callable[[dict], object] | None = None,
    client: OpenAI | None = None,
    meter: UsageMeter | None = None,
    super_model: str | None = None,
    ultra_model: str | None = None,
) -> list[dict]:
    """Invent `n` inputs with Super and, for graded-output sites, label each.

    Labelling uses `labeller` if given (e.g. running the app through its original model), otherwise Ultra.
    """
    client = client or tokenfactory.client()
    if super_model is None or (needs_label and labeller is None and ultra_model is None):
        tiers = tokenfactory.tiers()
        super_model = super_model or tiers["super"]
        ultra_model = ultra_model or tiers["ultra"]

    def call(model: str, system: str, user: str) -> str:
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=8192,
        )
        if meter is not None:
            meter.record(model, response.usage, time.perf_counter() - start)
        return response.choices[0].message.content or ""

    inputs = parse_inputs(call(super_model, INPUT_SYSTEM, input_prompt(title, function, param_names, source, n)), param_names)
    cases: list[dict] = []
    for i, inp in enumerate(inputs, start=1):
        expected: object = {}
        if needs_label:
            if labeller is not None:
                try:
                    expected = labeller(inp)
                except Exception:  # a case that can't be labelled is dropped, not fatal
                    continue
            else:
                user = f"Call source:\n<<<\n{source[:6000]}\n>>>\n\nInput:\n{json.dumps(inp, ensure_ascii=False)}"
                expected = parse_label(call(ultra_model, LABEL_SYSTEM, user))
                if expected is None:
                    continue
        cases.append({"id": f"{site_id}-{i:02d}", "input": inp, "expected": expected})
    return cases
