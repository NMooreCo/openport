"""The PR description: what moved where, how it was tested, what it costs, and what to check before merging."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import config
from .pricing import Price
from .tune.variant import Variant

TITLE = "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory"
SUMMARY_FILE = "openport.yaml"


@dataclass
class Row:
    id: str
    file: str
    line: int
    status: str  # ported | kept
    was: str | None
    now: str | None
    reason: str = ""
    check: list[str] = field(default_factory=list)
    plain: float | None = None  # Nano with the app's prompt as written
    after: float | None = None
    held_out: float | None = None
    cases: int | None = None
    tier: str | None = None
    change: str = ""
    addendum: str = ""
    sources: list[dict] = field(default_factory=list)  # NVIDIA guidance the tuned change relied on
    per_call_tokens: tuple[float, float] | None = None  # (prompt, completion), measured on Nemotron
    cost_was: float | None = None  # USD per 1,000 calls
    cost_now: float | None = None


def load_summary(ported: Path) -> list[dict]:
    return yaml.safe_load((ported / SUMMARY_FILE).read_text(encoding="utf-8"))["call_sites"]


def latest_tune_report(target: str) -> Path | None:
    reports = sorted(config.RUNS_DIR.glob(f"tune-{target}-*.json"))
    return reports[-1] if reports else None


def _tuning_for(site: dict, results: list[dict]) -> dict | None:
    """The tuning result for a ported call site: same file, same function."""
    function = site["id"].split(".")[-1].split("-")[0]
    return next((r for r in results if r["file"] == site["file"] and r["function"].split(".")[-1] == function), None)


def build_rows(sites: list[dict], tune_report: dict | None, price_of: Callable[[str], Price | None]) -> list[Row]:
    results = (tune_report or {}).get("results", [])
    rows = []
    for site in sites:
        ported = site["status"] == "ported"
        row = Row(
            id=site["id"],
            file=site["file"],
            line=site["line"],
            status=site["status"],
            was=site.get("was") if ported else site.get("model"),
            now=site.get("model") if ported else None,
            reason=site.get("reason", ""),
            check=site.get("check", []),
        )
        tuning = _tuning_for(site, results) if ported else None
        if tuning and tuning["attempts"]:
            row.plain = _rate(tuning["attempts"][0])
            chosen = tuning.get("chosen")
            if chosen:
                row.after, row.held_out, row.cases, row.tier = _rate(chosen), chosen["holdout_pass"], chosen["total"], chosen["tier"]
                variant = Variant.from_dict(chosen["variant"])
                row.change, row.addendum, row.sources = variant.describe(), variant.system_addendum, variant.sources
                if chosen["total"]:
                    row.per_call_tokens = (chosen["prompt_tokens"] / chosen["total"], chosen["completion_tokens"] / chosen["total"])
        if row.per_call_tokens and row.was and row.now:
            was_price, now_price = price_of(row.was), price_of(row.now)
            if was_price:
                row.cost_was = was_price.cost(*row.per_call_tokens) * 1000
            if now_price:
                row.cost_now = now_price.cost(*row.per_call_tokens) * 1000
        rows.append(row)
    return rows


def _rate(attempt: dict) -> float:
    return attempt["passed"] / attempt["total"] if attempt["total"] else 0.0


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    if value is None:
        return "?"
    return f"${value:.2f}" if value >= 0.1 else f"${value:.4f}"


def render(target: str, rows: list[Row], tune_report: dict | None) -> str:
    ported = [r for r in rows if r.status == "ported"]
    kept = [r for r in rows if r.status == "kept"]
    tuned = any(r.after is not None for r in ported)
    lines = [
        f"## {TITLE}",
        "",
        f"This PR moves **{len(ported)} of {len(rows)}** LLM calls in `{target}` from closed APIs to open NVIDIA "
        "Nemotron models served by Nebius Token Factory.",
    ]
    if tuned:
        target_rate = tune_report.get("target_pass_rate", 0.9)
        lines.append(
            f"Every ported call was tested before and after. A call only moved if it passed {target_rate:.0%} of its "
            "test cases, including cases held out from tuning."
        )
    lines += ["", "| Call | Was | Now | Plain swap | After tuning | Held-out | Est. cost per 1k calls |", "|---|---|---|---|---|---|---|"]
    for r in ported:
        cost = f"{_usd(r.cost_was)} → {_usd(r.cost_now)}" if r.cost_was is not None or r.cost_now is not None else "-"
        cases = f" ({r.cases} cases)" if r.cases else ""
        lines.append(
            f"| `{r.id}` ([{r.file}:{r.line}]({r.file}#L{r.line})) | `{r.was}` | `{r.now}` | {_pct(r.plain)} | "
            f"{_pct(r.after)}{cases} | {_pct(r.held_out)} | {cost} |"
        )
    costs = [r for r in ported if r.cost_was is not None and r.cost_now is not None]
    if costs and len(costs) == len(ported):
        before, after = sum(r.cost_was for r in costs), sum(r.cost_now for r in costs)
        saving = f" ({(before - after) / before:.0%} less)" if before > 0 and after < before else ""
        lines += ["", f"**Estimated cost for 1,000 runs of each call: {_usd(before)} → {_usd(after)}{saving}.** "
                  "Estimated from token counts measured on Nemotron, at list prices."]

    if kept:
        lines += ["", "**Kept on the original provider:**"]
        lines += [f"- `{r.id}` ({r.file}:{r.line}): {r.reason}" for r in kept]

    changes = [r for r in ported if r.change and r.change != "as written"]
    lines += ["", "### What changed", "",
              "- Each SDK client now points at Token Factory (`NEBIUS_API_KEY`). Code written for the Anthropic SDK runs "
              "through `openport_llm.py`, which translates Messages calls to chat completions.",
              "- Each call names its Nemotron model, with a `# was ...` comment showing the old one."]
    for r in changes:
        detail = f": “{r.addendum}”" if r.addendum else ""
        links = ", ".join(f"[{s['title']}]({s['url']})" for s in r.sources)
        cited = f" Follows NVIDIA's guidance: {links}." if links else ""
        lines.append(f"- `{r.id}` tuned for {r.tier} ({r.change}){detail}{cited}")

    checks = [(r.id, note) for r in ported for note in r.check]
    if checks:
        lines += ["", "### Check before merging", ""]
        lines += [f"- `{site}`: {note}" for site, note in checks]

    lines += ["", "### How this was tested", ""]
    if tuned:
        lines += [
            "- **Plain swap**: Nemotron Nano with the original prompt and settings, as if only the base URL and model had changed.",
            "- **After tuning**: the model and settings in this PR. Nemotron Ultra proposed the prompt and setting changes "
            "from failing cases. About 30% of cases were held out: Ultra never saw them, and a change only counted if it passed them too.",
            "- Grading uses strict checks first (exact labels, JSON fields, tool calls, required phrases); a Nemotron Ultra "
            "judge scores tone and helpfulness of free text.",
        ]
        if any(r.sources for r in ported):
            lines.append("- Before proposing a fix, OpenPort looked up NVIDIA's own guidance for the target model with Tavily; "
                         "changes that relied on it link the pages above.")
    else:
        lines.append("- Not tuned yet. Run `openport tune` with an eval spec to measure each call before and after.")
    lines += ["", "### Setup", "", "Set `NEBIUS_API_KEY` to a [Nebius Token Factory](https://tokenfactory.nebius.com) key.",
              "", "---", "Opened by OpenPort. *Prove it, then port it.*"]
    return "\n".join(lines) + "\n"


def load_tune_report(path: Path | None) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path else None
