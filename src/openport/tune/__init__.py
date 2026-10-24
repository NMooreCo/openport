"""Tuning: find the cheapest Nemotron model, and the smallest change, that makes each call site pass.

For each call site:
1. Run the eval on Nano with the app's prompt as written (the plain-swap baseline).
2. If it falls short, Ultra reads the failures and proposes a variant, and the eval runs again.
3. After a few rounds without passing, move up to Super, then Ultra, starting from the best variant so far.

About 30% of each call site's cases are held out: the engineer never sees them, and a variant only
counts as passing if it clears the bar on both the cases it was tuned on and the held-out ones.

With a Tavily key, the engineer also gets NVIDIA's own guidance for the model it's tuning for (see
research.py), and the variant records the pages it relied on.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from openai import OpenAI

from .. import config, tokenfactory
from ..evals.graders import Judge
from ..evals.runner import CaseResult, make_judge, run_site
from ..evals.spec import CallSite, Spec, load_spec
from ..runtime.openport_llm import UsageMeter
from .engineer import Engineer, make_engineer
from .research import Guidance, Researcher, make_researcher
from .variant import Variant, VariantClient

HOLDOUT_FRACTION = 0.3


@dataclass
class Attempt:
    tier: str
    model: str
    round: int
    variant: Variant
    tune_pass: float
    holdout_pass: float | None
    passed: int
    total: int
    cost_usd: float | None
    failed_cases: list[str] = field(default_factory=list)
    prompt_tokens: int = 0  # totals across all cases, as measured on Nemotron
    completion_tokens: int = 0
    cases: int = 0  # distinct cases; `passed` and `total` count every sample

    def meets(self, target: float) -> bool:
        return self.tune_pass >= target and (self.holdout_pass is None or self.holdout_pass >= target)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass
class SiteTuning:
    site_id: str
    title: str
    file: str
    function: str
    attempts: list[Attempt]
    chosen: Attempt | None
    system_prompts: list[str] = field(default_factory=list)  # every distinct system prompt the app sent
    research: list[Guidance] = field(default_factory=list)  # NVIDIA guidance looked up for this call

    @property
    def baseline(self) -> Attempt | None:
        return self.attempts[0] if self.attempts else None

    @property
    def best(self) -> Attempt | None:
        return self.chosen or max(self.attempts, key=lambda a: a.pass_rate, default=None)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd or 0 for a in self.attempts)


def split_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """A stable tune/holdout split: the same case always lands on the same side."""
    if len(cases) < 4:
        return cases, []
    ranked = sorted(cases, key=lambda c: hashlib.sha1(c["id"].encode()).hexdigest())
    held = {c["id"] for c in ranked[: max(1, round(len(cases) * HOLDOUT_FRACTION))]}
    return [c for c in cases if c["id"] not in held], [c for c in cases if c["id"] in held]


def _rate(results: list[CaseResult]) -> float:
    return sum(r.passed for r in results) / len(results) if results else 0.0


def tune_site(
    spec: Spec,
    site: CallSite,
    models: dict[str, str],
    *,
    client: OpenAI,
    engineer: Engineer,
    judge: Judge | None = None,
    target: float = 0.9,
    rounds: int = 3,
    tiers: tuple[str, ...] = config.TIERS,
    concurrency: int = 8,
    limit: int | None = None,
    on_attempt: Callable[[CallSite, Attempt], None] | None = None,
    researcher: Researcher | None = None,
    on_research: Callable[[CallSite, Guidance], None] | None = None,
) -> SiteTuning:
    cases = site.cases(limit)
    tune_cases, holdout = split_cases(cases)
    held_ids = {c["id"] for c in holdout}
    by_id = {c["id"]: c for c in cases}
    attempts: list[Attempt] = []
    best: Attempt | None = None
    system_prompts: list[str] = []
    research: list[Guidance] = []

    def finish(chosen: Attempt | None) -> SiteTuning:
        return SiteTuning(site.id, site.title, site.file, site.function, attempts, chosen, system_prompts, research)

    for tier in tiers:
        model = models[tier]
        variant = best.variant if best else Variant()
        for round_number in range(rounds):
            wrapped = VariantClient(client, variant)
            result = run_site(spec, site, tier, model, concurrency=concurrency, judge=judge, client=wrapped, cases=cases)
            system_prompts.extend(s for s in wrapped.systems if s not in system_prompts)
            tuned = [r for r in result.cases if r.case_id not in held_ids]
            held = [r for r in result.cases if r.case_id in held_ids]
            attempt = Attempt(
                tier=tier,
                model=model,
                round=round_number,
                variant=variant,
                tune_pass=_rate(tuned),
                holdout_pass=_rate(held) if held else None,
                passed=result.passed,
                total=len(result.cases),
                cost_usd=result.cost_usd,
                failed_cases=list(dict.fromkeys(r.case_id for r in result.cases if not r.passed)),
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cases=len({r.case_id for r in result.cases}),
            )
            attempts.append(attempt)
            if on_attempt:
                on_attempt(site, attempt)
            if best is None or attempt.pass_rate > best.pass_rate:
                best = attempt
            if attempt.meets(target):
                return finish(attempt)
            failures = [(by_id[r.case_id], r) for r in tuned if not r.passed]
            if round_number == rounds - 1 or not failures:
                break  # out of rounds, or only held-out cases fail (the engineer can't see those): move up a tier
            guidance = researcher(site, model, failures) if researcher else None
            if guidance is not None and guidance not in research:
                research.append(guidance)
                if on_research:
                    on_research(site, guidance)
            if guidance is None:
                variant = engineer(site, model, variant, wrapped.sample, failures)
            else:
                variant = engineer(site, model, variant, wrapped.sample, failures, guidance=guidance)
    return finish(None)


def _finding_ids(spec: Spec) -> dict[tuple[str, str], str]:
    """Map eval call sites to the scanner's call-site IDs (which `openport port` plans use)."""
    from ..scan import scan_repo

    return {(f.file, f.function): f.id for f in scan_repo(spec.source_root).portable()}


def export_plan(spec: Spec, results: list[SiteTuning], target: float) -> dict:
    ids = _finding_ids(spec)
    plan = {}
    for result in results:
        site_id = ids.get((result.file, result.function), result.site_id)
        if result.chosen is None:
            best = result.best
            reason = f"no Nemotron tier reached {target:.0%}" + (f" (best: {best.tier} at {best.pass_rate:.0%})" if best else "")
            plan[site_id] = {"model": "keep", "reason": reason}
            continue
        chosen = result.chosen
        entry = {
            "model": chosen.model,
            "tier": chosen.tier,
            "pass_rate": round(chosen.pass_rate, 3),
            "holdout_pass_rate": None if chosen.holdout_pass is None else round(chosen.holdout_pass, 3),
            "variant": chosen.variant.to_dict(),
        }
        if chosen.variant.reasoning is not None:
            entry["extra_body"] = {"chat_template_kwargs": {"enable_thinking": chosen.variant.reasoning}}
        if chosen.variant.system_addendum:
            entry["system_prompts"] = result.system_prompts  # lets the port find where each prompt is written
        plan[site_id] = entry
    return {"target": target, "call_sites": plan}


def tune_spec(
    target_dir: Path,
    *,
    site_ids: list[str] | None = None,
    target: float = 0.9,
    rounds: int = 3,
    tiers: tuple[str, ...] = config.TIERS,
    concurrency: int = 8,
    limit: int | None = None,
    use_judge: bool = True,
    use_research: bool = True,
    on_attempt: Callable[[CallSite, Attempt], None] | None = None,
    on_research: Callable[[CallSite, Guidance], None] | None = None,
) -> tuple[list[SiteTuning], dict, Path, Path]:
    spec = load_spec(target_dir)
    sites = [s for s in spec.call_sites if not site_ids or s.id in site_ids]
    models = tokenfactory.tiers()
    client = tokenfactory.client()
    helper_meter = UsageMeter()  # the engineer's and the judge's own calls: what OpenPort itself costs
    engineer = make_engineer(client, meter=helper_meter)
    judge = make_judge(helper_meter, client=client) if use_judge else None
    researcher = make_researcher() if use_research and os.getenv("TAVILY_API_KEY") else None

    results = [
        tune_site(
            spec, site, models, client=client, engineer=engineer, judge=judge, target=target, rounds=rounds,
            tiers=tiers, concurrency=concurrency, limit=limit, on_attempt=on_attempt,
            researcher=researcher, on_research=on_research,
        )
        for site in sites
    ]
    helper_cost = sum(
        (tokenfactory.model_info(model).cost(u.prompt_tokens, u.completion_tokens) or 0) if tokenfactory.model_info(model) else 0
        for model, u in helper_meter.by_model.items()
    )
    plan = export_plan(spec, results, target)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = config.RUNS_DIR / f"tune-{spec.target}-{stamp}.json"
    report_path.write_text(
        json.dumps(
            {
                "kind": "tune",
                "target": spec.target,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "target_pass_rate": target,
                "models": {tier: models[tier] for tier in tiers},
                "helper_cost_usd": helper_cost,
                "results": [asdict(r) for r in results],
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    plan_path = config.RUNS_DIR / f"plan-{spec.target}.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return results, {"helper_cost_usd": helper_cost}, report_path, plan_path
