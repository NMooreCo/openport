"""Run a call site's eval cases on a Nemotron model, through the OpenPort runtime, and grade them."""

import asyncio
import importlib
import inspect
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI
from pydantic import BaseModel

from .. import tokenfactory
from ..runtime.openport_llm import AnthropicOnNemotron, SwappedOpenAI, UsageMeter
from .context import CURRENT_CASE
from .graders import Judge, grade
from .intercept import http_routed, routed
from .node import EvalProxy, run_node_case
from .spec import CallSite, Spec


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    score: float
    failures: list[str]
    output: Any
    error: str | None
    seconds: float


@dataclass
class SiteResult:
    site_id: str
    title: str
    tier: str
    model: str
    cases: list[CaseResult]
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    answer_tokens: int = 0  # the completion tokens of the replies alone, without Nemotron's reasoning

    @property
    def passed(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.cases) if self.cases else 0.0

    @property
    def mean_score(self) -> float:
        return sum(case.score for case in self.cases) / len(self.cases) if self.cases else 0.0

    def error_types(self) -> Counter:
        """Crashes by exception type, e.g. JSONDecodeError: 3."""
        return Counter(case.error.split(":", 1)[0] for case in self.cases if case.error)


def _plain(output: Any) -> Any:
    return output.model_dump() if isinstance(output, BaseModel) else output


def _settle(result: Any) -> Any:
    """An async entry point's coroutine, run to its result."""
    return asyncio.run(result) if inspect.iscoroutine(result) else result


def prepare_imports(spec: Spec) -> None:
    # Apps like SupportDesk build their SDK clients at import time, which fails without a key.
    # OpenPort swaps those clients out before any call, so placeholders are enough.
    os.environ.setdefault("OPENAI_API_KEY", "openport-placeholder")
    os.environ.setdefault("ANTHROPIC_API_KEY", "openport-placeholder")
    for path in [*spec.extra_paths, spec.source_root]:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def swapped_client(site: CallSite, model: str | None, meter: UsageMeter, client: OpenAI, extra_body: dict | None = None):
    """A Token Factory client shaped like the site's SDK. With `model=None`, each call keeps the model it names."""
    if site.sdk == "openai":
        return SwappedOpenAI(model or (lambda named: named), client=client, meter=meter, extra_body=extra_body)
    if site.sdk == "anthropic":
        return AnthropicOnNemotron(model, client=client, meter=meter, extra_body=extra_body)
    raise ValueError(f"Unsupported SDK {site.sdk!r} for call site {site.id}")


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    try:
        info = tokenfactory.model_info(model)
    except Exception:  # pricing is nice to have; never fail a run over it
        return None
    return info.cost(prompt_tokens, completion_tokens) if info else None


def _run_case(site: CallSite, call: Callable[[dict], Any], case: dict, judge: Judge | None) -> CaseResult:
    start = time.perf_counter()
    token = CURRENT_CASE.set(case["id"])
    try:
        output = _plain(call(case))
    except Exception as error:  # a crash is a failed case, not a failed run
        seconds = round(time.perf_counter() - start, 2)
        return CaseResult(case["id"], False, 0.0, [], None, f"{type(error).__name__}: {error}"[:300], seconds)
    finally:
        CURRENT_CASE.reset(token)
    seconds = round(time.perf_counter() - start, 2)
    try:
        result = grade(site.grader, output, case["expected"], case["input"], judge)
    except Exception as error:  # the output isn't the shape the grader expects
        return CaseResult(case["id"], False, 0.0, [], output, f"WrongShape: {type(error).__name__}: {error}"[:300], seconds)
    return CaseResult(case["id"], result.passed, result.score, result.failures, output, None, seconds)


def run_site(
    spec: Spec,
    site: CallSite,
    tier: str,
    model: str | None,
    *,
    concurrency: int = 8,
    limit: int | None = None,
    judge: Judge | None = None,
    extra_body: dict | None = None,
    client: OpenAI | None = None,
    cases: list[dict] | None = None,
) -> SiteResult:
    """Point the call site's SDK client at `model`, run every case, and put the original client back.

    While a case runs, the SDK's own calls go to the swapped client, whatever client object the code
    built (evals/intercept.py). `client_attr` can also name the module attribute the code gets its
    client from: a client object (`client`), or a factory function (`get_client()`), which is replaced
    by one returning the swapped client; `intercept: true` sites have none. An async entry point runs
    to completion. With `model=None`, each call runs on the model its code names.

    A TypeScript or JavaScript site runs under Node instead, its OpenAI SDK pointed at a local proxy
    that sends each request through the same swapped client (see evals/node.py).
    """
    meter = UsageMeter()
    real = client or tokenfactory.client()
    # Requests that reach the local proxy (TypeScript, plain HTTP calls) may use either API: code ported off
    # Anthropic's SDK sends chat completions, for one.
    chat = SwappedOpenAI(model or (lambda named: named), client=real, meter=meter, extra_body=extra_body)
    messages = AnthropicOnNemotron(model, client=real, meter=meter, extra_body=extra_body)
    swapped = messages if site.sdk == "anthropic" else chat
    cases = cases if cases is not None else site.cases(limit)
    cases = [case for case in cases for _ in range(site.samples)]  # every sample is graded and counted
    if site.runs_on_node:
        with EvalProxy(chat, messages) as proxy, ThreadPoolExecutor(max_workers=concurrency) as pool:
            def call(case: dict):
                return run_node_case(spec.source_root, site.module, site.entry_function, site.args or None, case["input"], proxy.base_url(case["id"]))

            results = list(pool.map(lambda case: _run_case(site, call, case, judge), cases))
    else:
        prepare_imports(spec)
        function = getattr(importlib.import_module(site.module), site.entry_function)
        proxy = EvalProxy(chat, messages) if site.sdk == "http" else None  # plain HTTP calls are redirected to it

        def call(case: dict):
            if proxy is not None:
                with http_routed(proxy.base_url(case["id"])):
                    return _settle(function(**case["input"]))
            with routed(swapped, passthrough=real):  # the swapped client's own calls to Token Factory pass through
                return _settle(function(**case["input"]))

        home = None if site.intercept or proxy is not None else importlib.import_module(site.client_home)
        attr = site.client_attr.removesuffix("()")
        original = getattr(home, attr) if home is not None else None
        if home is not None:
            setattr(home, attr, (lambda *args, **kwargs: swapped) if site.client_attr.endswith("()") else swapped)
        try:
            with proxy or nullcontext(), ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(lambda case: _run_case(site, call, case, judge), cases))
        finally:
            if home is not None:
                setattr(home, attr, original)

    used = meter.by_model
    prompt_tokens = sum(u.prompt_tokens for u in used.values())
    completion_tokens = sum(u.completion_tokens for u in used.values())
    costs = [_cost(m, u.prompt_tokens, u.completion_tokens) for m, u in used.items()]
    return SiteResult(
        site_id=site.id,
        title=site.title,
        tier=tier,
        model=model or ", ".join(used) or "as written",
        cases=results,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=None if any(c is None for c in costs) else sum(costs),
        answer_tokens=sum(u.answer_tokens for u in used.values()),
    )


_SCORE = re.compile(r'"score"\s*:\s*"?([1-5])')


def make_judge(meter: UsageMeter | None = None, client: OpenAI | None = None, model: str | None = None) -> Judge:
    """An LLM judge on Nemotron Ultra that scores free-text outputs from 1 to 5 against a rubric."""
    client = client or tokenfactory.client()
    model = model or tokenfactory.tiers()["ultra"]

    def judge(rubric: str, case_input: dict, output: str) -> int:
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": rubric + '\n\nRespond with JSON only: {"score": <1-5>, "reason": "<one sentence>"}'},
                {
                    "role": "user",
                    "content": f"What the writer was given:\n{json.dumps(case_input, ensure_ascii=False, indent=2)}"
                    f"\n\nDraft to score:\n{output}",
                },
            ],
            max_tokens=4096,
        )
        if meter is not None:
            meter.record(model, response.usage, time.perf_counter() - start)
        match = _SCORE.search(response.choices[0].message.content or "")
        if match is None:
            raise ValueError("judge returned no score")
        return int(match.group(1))

    return judge
