"""The dashboard's pipeline: fetch, scan, tune, port and write the PR, reporting each step as an event."""

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .. import config, pricing, report, tokenfactory
from ..capture import capture_repo, write_capture
from ..evals.spec import SPEC_FILE, load_spec
from ..port import build_plan, load_assignments, port_repo
from ..port import find_eval_spec
from ..scan import scan_repo
from ..tune import tune_spec
from ..workspace import fetch_repo

EXAMPLES = Path(os.getenv("OPENPORT_EXAMPLES", Path(__file__).resolve().parents[3] / "examples"))
PRESETS = [
    {"id": "supportdesk", "label": "SupportDesk", "detail": "Demo app: 5 calls on OpenAI + Anthropic, 118 labelled test cases", "path": "supportdesk"},
    {"id": "preflight", "label": "jsliapark/preflight", "detail": "AI PR reviewer: 4 Claude Opus calls, 119 labelled test cases", "repo": "jsliapark/preflight"},
    {"id": "probly", "label": "PragmaticMachineLearning/probly", "detail": "Spreadsheet assistant: 4 GPT-4o calls, tools and vision", "repo": "PragmaticMachineLearning/probly"},
]
GITHUB_REPO = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9][\w.-]*/[A-Za-z0-9_][\w.-]*?)(?:\.git)?/?$")
REUSE_SECONDS = int(os.getenv("OPENPORT_REUSE_SECONDS", "1800"))  # rerunning a target this soon reuses the result
AUTO_CAPTURE = os.getenv("OPENPORT_AUTO_CAPTURE", "1") != "0"  # with no eval spec, generate a starter one
CAPTURE_CASES = int(os.getenv("OPENPORT_CAPTURE_CASES", "6"))  # cases per call when capturing
# Testing a call imports the repo's code and runs it, in this process, next to the API keys. A public server sets
# "vetted": then only the bundled demo apps and repos with an OpenPort eval pack (pinned to a reviewed commit) run.
VETTED_ONLY = os.getenv("OPENPORT_RUN_REPO_CODE", "any") == "vetted"

# Tuning swaps SDK clients inside imported modules, which is process-wide: one pipeline at a time.
PIPELINE_LOCK = threading.Lock()


@dataclass
class Run:
    id: str
    target: str
    label: str
    status: str = "queued"  # queued | running | done | error
    created: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    kind: str = "port"  # port | upgrade

    def emit(self, kind: str, **data) -> None:
        self.events.append({"seq": len(self.events), "type": kind, "time": round(time.time(), 3), **data})

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target, "label": self.label, "status": self.status, "created": self.created}


@dataclass
class EvalPack:
    folder: Path
    source: str  # the repo it tests, as `source_root` names it: owner/name, or owner/name@<commit> when pinned

    @property
    def commit(self) -> str | None:
        return self.source.partition("@")[2] or None


def eval_pack_for(target: str) -> EvalPack | None:
    """OpenPort's own eval spec for a public repo (examples/evalpacks/*, matched on `source_root`)."""
    packs = EXAMPLES / "evalpacks"
    for spec in sorted(packs.glob(f"*/{SPEC_FILE}")) if packs.is_dir() else []:
        root = str((yaml.safe_load(spec.read_text(encoding="utf-8")) or {}).get("source_root", ""))
        if root.startswith("repo:") and root.removeprefix("repo:").partition("@")[0] == target:
            return EvalPack(spec.parent, root.removeprefix("repo:"))
    return None


def tests_target(target: str, local: Path | None) -> bool:
    """Whether a run tests the target's calls on this server, which means running its code (see VETTED_ONLY)."""
    if local is not None or not VETTED_ONLY:
        return True
    pack = eval_pack_for(target)
    return bool(pack and pack.commit)


def resolve_target(target: str, presets: list[dict] | None = None) -> tuple[str, str, Path | None]:
    """(normalized target, label, local path for presets). Only presets and public GitHub repos are accepted."""
    preset = next((p for p in (PRESETS if presets is None else presets) if target in (p["id"], p.get("repo"))), None)
    if preset and preset.get("path"):
        return preset["id"], preset["label"], EXAMPLES / preset["path"]
    if preset:
        return preset["repo"], preset["label"], None
    match = GITHUB_REPO.match(target.strip())
    if match is None:
        raise ValueError("Use a demo preset or a public GitHub repo as owner/name")
    return match[1], match[1], None


def _attempt_event(site_id: str, title: str, file: str, function: str, attempt, target: float) -> dict:
    return {
        "site": site_id,
        "title": title,
        "file": file,
        "function": function,
        "tier": attempt.tier,
        "model": attempt.model,
        "round": attempt.round,
        "variant": attempt.variant.describe(),
        "addendum": attempt.variant.system_addendum,
        "rationale": attempt.variant.rationale,
        "sources": attempt.variant.sources,
        "tune_pass": attempt.tune_pass,
        "holdout_pass": attempt.holdout_pass,
        "pass_rate": attempt.pass_rate,
        "passed": attempt.passed,
        "total": attempt.total,
        "cost_usd": attempt.cost_usd,
        "meets": attempt.meets(target),
    }


def _capture_spec(run: Run, root: Path, name: str, scan) -> Path | None:
    """With no eval spec, generate a starter one so the calls can still be tuned. Unreviewed, so it's flagged."""
    run.emit("stage", stage="capture")
    result = capture_repo(
        root, scan, generate=CAPTURE_CASES,
        on_site=lambda site: run.emit("captured_site", site=site.spec["id"], cases=len(site.cases), source=site.source),
    )
    result.sites = [site for site in result.sites if site.cases]  # only tune calls that actually have cases
    if not result.sites:
        run.emit("note", message="Couldn't generate a usable eval set for this repo, so each call moves to Super untuned.")
        return None
    out = config.RUNS_DIR / "captures" / name
    write_capture(result, out)
    run.emit(
        "captured",
        sites=[{"site": s.spec["id"], "cases": len(s.cases), "grader": s.spec["grader"]["type"]} for s in result.sites],
        skipped=[{"site": sid, "reason": reason} for sid, reason in result.skipped],
        helper_usd=result.helper_cost_usd,
        note="This eval set was generated by Nemotron Super and Ultra and has not been reviewed; treat the scores as indicative.",
    )
    return out


def run_pipeline(run: Run, target: str, local: Path | None, target_rate: float = 0.9) -> None:
    with PIPELINE_LOCK:
        run.status = "running"
        try:
            _pipeline(run, target, local, target_rate)
            run.status = "done"
            run.emit("done")
        except Exception as error:  # surface every failure to the dashboard
            run.status = "error"
            run.emit("failed", message=f"{type(error).__name__}: {error}")
        _save(run)


def _pipeline(run: Run, target: str, local: Path | None, target_rate: float) -> None:
    pack = eval_pack_for(target) if local is None else None
    run.emit("stage", stage="fetch")
    if local is not None:
        root = local
    else:
        # A repo with an eval pack is fetched at the commit the pack was reviewed against, so the port is what was tested.
        root, fresh = fetch_repo(pack.source if pack else target)
        at = f" at {pack.commit[:7]}" if pack and pack.commit else ""
        run.emit("note", message=f"{'Cloned' if fresh else 'Reused a clone of'} github.com/{target}{at}. Scanning parses its code without running it.")
    name = root.resolve().name

    run.emit("stage", stage="scan")
    scan = scan_repo(root)
    sites = scan.portable()
    run.emit(
        "scan",
        files=scan.files_scanned,
        summary=scan.summary(),
        sites=[{**asdict(f), "portability": f.portability, "notes": f.notes} for f in sites],
    )
    if not sites:
        run.emit("note", message="No OpenAI or Anthropic SDK calls found, so there's nothing to port.")
        return

    models = tokenfactory.tiers()
    run.emit("models", models=models)

    runs_any_code = local is not None or not VETTED_ONLY
    spec_rel = find_eval_spec(root) if runs_any_code else None  # a repo's own spec runs whatever it names
    spec_target = (root / spec_rel) if spec_rel else None
    if spec_target is None and pack is not None and tests_target(target, local):
        spec_target = pack.folder
        cases = sum(len(s.cases()) for s in load_spec(pack.folder).call_sites)
        run.emit("note", message=f"OpenPort tests this repo with its own eval pack: {cases} hand-labelled cases.")
    if spec_target is None and AUTO_CAPTURE and runs_any_code:
        spec_target = _capture_spec(run, root, name, scan)

    assignments, tune_data = {}, None
    if spec_target is not None:
        run.emit("stage", stage="tune")
        results, totals, report_path, plan_path = tune_spec(
            spec_target,
            target=target_rate,
            on_attempt=lambda site, attempt: run.emit(
                "attempt", **_attempt_event(site.id, site.title, site.file, site.function, attempt, target_rate)
            ),
            on_research=lambda site, guidance: run.emit(
                "research", site=site.id, file=site.file, function=site.function, model=guidance.model, topic=guidance.topic,
                query=guidance.query, sources=[{"title": s["title"], "url": s["url"]} for s in guidance.sources],
            ),
        )
        for r in results:
            run.emit(
                "tuned",
                site=r.site_id,
                file=r.file,
                function=r.function,
                chosen=None if r.chosen is None else _attempt_event(r.site_id, r.title, r.file, r.function, r.chosen, target_rate),
                plain=r.baseline.pass_rate if r.baseline else None,
            )
        run.emit("cost", evals_usd=sum(r.cost_usd for r in results), helper_usd=totals["helper_cost_usd"])
        assignments = load_assignments(plan_path)
        tune_data = report.load_tune_report(report_path)
    elif runs_any_code:
        run.emit("note", message="This repo has no eval spec yet, so each call moves to Nemotron Super untuned.")
    else:
        run.emit("note", message="Testing a call runs the repo's code, which this public demo does only for its demo repos, so each "
                                 "call moves to Nemotron Super untuned. Run `openport capture` and `openport tune` locally to test them.")

    run.emit("stage", stage="port")
    plan = build_plan(scan.findings, models, "super", assignments)
    result = port_repo(root, scan, plan, config.RUNS_DIR / "ports" / name, min_pass_rate=target_rate)
    run.emit(
        "ported",
        sites=[asdict(s) for s in result.sites],
        changed=result.changed,
        added=result.added,
        diff=result.diff[:300_000],
    )

    run.emit("stage", stage="pr")
    rows = report.build_rows(report.load_summary(result.output), tune_data, pricing.price_for)
    run.emit("pr", body=report.render(target, rows, tune_data), rows=[asdict(r) for r in rows])


def _save(run: Run) -> None:
    folder = config.RUNS_DIR / "dashboard"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{run.id}.json").write_text(json.dumps({**run.summary(), "events": run.events}, default=str), encoding="utf-8")
