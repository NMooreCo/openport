"""Distill: build a fine-tuning set for a call site from Nemotron Ultra's own answers ("Ultra teaches a smaller model").

- New inputs are invented by Nemotron Super from the app's source, never taken from the eval cases, so a
  fine-tuned student is still tested on cases it hasn't seen.
- Ultra answers them through the app's own code, with the best settings tuning found for Ultra. Each
  training example is the chat request the app itself sent (before any tuned prompt addition, so the
  student learns to follow the app's real prompt) plus Ultra's answer.
- Only answers that pass the call's own checks are kept; for free text, its format rules and the judge.
  Closed-model outputs are never used: their terms forbid training competing models on them.
"""

import importlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAI

from . import config, tokenfactory
from .capture.draft import param_names
from .capture.generate import INPUT_SYSTEM, input_prompt, parse_inputs
from .evals.graders import Judge, grade
from .evals.runner import make_judge, prepare_imports, swapped_client
from .evals.spec import CallSite, Spec, load_spec
from .runtime.openport_llm import UsageMeter
from .tune.variant import Variant, VariantClient

SKIPPED_OUTPUTS = {"tools"}  # multi-turn tool loops don't fit a single chat example
BATCH = 20  # inputs per Super request


@dataclass
class DistillResult:
    site: str
    teacher: str
    variant: dict
    invented: int = 0
    kept: int = 0
    rejected: dict = field(default_factory=dict)  # reason -> count
    cost_usd: float | None = None
    dataset: str = ""


class _Recorder:
    """Wraps a chat client and records each request (as the app sent it) and its response, per thread."""

    def __init__(self, client, local: threading.local):
        self._client, self._local = client, local
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create, parse=self._parse))

    def _record(self, method: str, request: dict):
        response = getattr(self._client.chat.completions, method)(**request)
        self._local.calls.append((request, response))
        return response

    def _create(self, **request):
        return self._record("create", request)

    def _parse(self, **request):
        return self._record("parse", request)


def invent_inputs(spec: Spec, site: CallSite, n: int, client: OpenAI, model: str, exclude: set[str], meter: UsageMeter) -> list[dict]:
    """Ask Super for `n` new realistic inputs, in batches, dropping duplicates and anything in the eval set."""
    source = (spec.source_root / site.file).read_text(encoding="utf-8", errors="replace")
    params = param_names(spec.source_root, SimpleNamespace(file=site.file, function=site.function))
    seen, inputs = set(exclude), []
    for _ in range(max(1, -(-n // BATCH)) * 2):  # allow some batches to come back short or repeated
        if len(inputs) >= n:
            break
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": INPUT_SYSTEM},
                      {"role": "user", "content": input_prompt(site.title, site.function, params, source, BATCH)}],
            max_tokens=8192,
            temperature=1.0,  # variety matters more than precision here
        )
        meter.record(model, response.usage, time.perf_counter() - start)
        for item in parse_inputs(response.choices[0].message.content or "", params):
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                inputs.append(item)
    return inputs[:n]


def best_variant(report: dict | None, site_id: str, tier: str = "ultra") -> Variant:
    """The settings that scored best for this site on the teacher's tier in a tuning report, else as written."""
    result = next((r for r in (report or {}).get("results", []) if r["site_id"] == site_id), None)
    attempts = [a for a in (result or {}).get("attempts", []) if a["tier"] == tier]
    if not attempts:
        return Variant()
    best = max(attempts, key=lambda a: a["passed"] / a["total"] if a["total"] else 0)
    return Variant.from_dict(best["variant"])


def label_free_check(site: CallSite, output, case_input: dict, judge: Judge | None) -> tuple[bool, str]:
    """Checks that need no expected answer: for free text, the call's format rules and the judge."""
    if output in (None, ""):
        return False, "empty output"
    if site.grader.get("type") == "text_rules":
        result = grade(site.grader, output, {}, case_input, judge)
        return result.passed, (result.failures[0] if result.failures else "")
    return True, ""


def distill_site(
    target_dir: Path,
    site_id: str,
    *,
    examples: int = 200,
    teacher_tier: str = "ultra",
    tune_report: dict | None = None,
    concurrency: int = 8,
    client: OpenAI | None = None,
    judge: Judge | None = None,
    on_progress=None,
) -> tuple[DistillResult, Path]:
    spec = load_spec(target_dir)
    site = next((s for s in spec.call_sites if s.id == site_id), None)
    if site is None:
        raise KeyError(f"No call site {site_id!r} in {spec.target}'s eval spec")
    if site.output in SKIPPED_OUTPUTS:
        raise ValueError(f"{site_id} is a tool-calling loop; distill supports single-response calls")

    client = client or tokenfactory.client()
    models = tokenfactory.tiers()
    teacher = models[teacher_tier]
    meter = UsageMeter()
    judge = judge if judge is not None else make_judge(meter, client=client)
    variant = best_variant(tune_report, site_id, teacher_tier)

    exclude = {json.dumps(case["input"], sort_keys=True, ensure_ascii=False) for case in site.cases()}
    inputs = invent_inputs(spec, site, examples, client, models["super"], exclude, meter)
    if on_progress:
        on_progress("invented", len(inputs))

    prepare_imports(spec)
    function = getattr(importlib.import_module(site.module), site.entry_function)
    module = importlib.import_module(site.client_home)
    local = threading.local()
    recorder = _Recorder(VariantClient(client, variant), local)  # records the app's request before the variant
    factory = site.client_attr.endswith("()")
    attr = site.client_attr.removesuffix("()")
    original = getattr(module, attr)
    swapped = swapped_client(site, teacher, meter, recorder)
    setattr(module, attr, (lambda *args, **kwargs: swapped) if factory else swapped)

    def teach(case_input: dict):
        local.calls = []
        try:
            output = function(**case_input)
            output = output.model_dump() if hasattr(output, "model_dump") else output
        except Exception as error:  # a crash is a rejected example, not a failed run
            return None, f"crashed: {type(error).__name__}"
        ok, reason = label_free_check(site, output, case_input, judge)
        if not ok:
            return None, reason or "failed the call's checks"
        if len(local.calls) != 1:
            return None, f"made {len(local.calls)} model calls"
        request, response = local.calls[0]
        answer = response.choices[0].message.content or ""
        return {"messages": [*request["messages"], {"role": "assistant", "content": answer}]}, ""

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outcomes = list(pool.map(teach, inputs))
    finally:
        setattr(module, attr, original)

    kept = [example for example, _ in outcomes if example]
    rejected: dict[str, int] = {}
    for example, reason in outcomes:
        if example is None:
            rejected[reason] = rejected.get(reason, 0) + 1
    costs = []
    for model, usage in meter.by_model.items():
        info = tokenfactory.model_info(model)
        costs.append(info.cost(usage.prompt_tokens, usage.completion_tokens) if info else None)

    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"distill-{spec.target}-{site_id}.jsonl"
    path.write_text("".join(json.dumps(example, ensure_ascii=False) + "\n" for example in kept), encoding="utf-8")
    result = DistillResult(
        site=site_id, teacher=teacher, variant=variant.to_dict(), invented=len(inputs), kept=len(kept), rejected=rejected,
        cost_usd=None if any(c is None for c in costs) else sum(costs), dataset=str(path),
    )
    path.with_suffix(".json").write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    return result, path
