"""Plain-swap baseline: change only the base URL and the model name, then measure what breaks.

OpenAI SDK calls run unchanged against Token Factory. Anthropic SDK calls go through a mechanical
translation (Token Factory has no Anthropic-compatible API) with no prompt changes. This is the
"before" number that OpenPort's tuning has to beat.
"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, tokenfactory
from .evals.runner import SiteResult, make_judge, run_site
from .evals.spec import load_spec
from .runtime.openport_llm import UsageMeter


def run_baseline(
    target: Path,
    tiers: list[str],
    *,
    concurrency: int = 8,
    limit: int | None = None,
    use_judge: bool = True,
    on_result: Callable[[SiteResult], None] | None = None,
) -> tuple[list[SiteResult], Path]:
    from .tune import _findings, tiers_for

    spec = load_spec(target)
    models = tokenfactory.tiers()
    client = tokenfactory.client()
    judge_meter = UsageMeter()
    judge = make_judge(judge_meter, client=client) if use_judge else None
    findings = _findings(spec)
    ladders = {site.id: tiers_for(site, findings, tuple(tiers), models) for site in spec.call_sites}  # images: the vision model

    results = []
    for site in spec.call_sites:
        for tier in ladders[site.id]:
            result = run_site(spec, site, tier, models[tier], concurrency=concurrency, limit=limit, judge=judge, client=client)
            results.append(result)
            if on_result:
                on_result(result)

    report = {
        "kind": "baseline",
        "target": spec.target,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "models": {tier: models[tier] for ladder in (tiers, *ladders.values()) for tier in ladder},
        "judge_usage": {model: asdict(usage) for model, usage in judge_meter.by_model.items()},
        "results": [
            {**asdict(result), "pass_rate": result.pass_rate, "mean_score": result.mean_score} for result in results
        ],
    }
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"baseline-{spec.target}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return results, path
