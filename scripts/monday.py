"""Once Token Factory has credit again: re-measure the demo apps, re-record the dashboard's demo runs, and fetch the
fine-tuned adapter, as one checked sequence (PLAN.md's Monday list, and docs/VIDEO.md's numbers).

Every step but the first spends Token Factory credit, so with no options the script only lists them. With --run it
asks first, totals the spend each step's own reports record, and stops once that passes --budget. A recorded run
is copied into examples/recorded/ (which ships in the public image) only if it holds no local path or key.

Usage: uv run python scripts/monday.py                         # list the steps; spends nothing
       uv run python scripts/monday.py --run [--yes]           # run them all, asking first unless --yes
       uv run python scripts/monday.py --run --from pr-preflight --budget 5
"""

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
RECORDED = ROOT / "examples" / "recorded"
PREFLIGHT = "jsliapark/preflight@7a00fa2928acea9fcddce9661417e2bd6e6c1403"  # the commit its eval pack was reviewed at
FINETUNE_JOB = "ftjob-ba4f5926762e41f2ad0e612e9c9516ca"  # succeeded Oct 24; its adapter waits on funds (402)
# What a recording mustn't carry into the public image: a home folder, or anything shaped like a key.
PRIVATE = re.compile(r"C:\\\\Users|/home/|/Users/|sk-[A-Za-z0-9]{12,}|tvly-[A-Za-z0-9]{8,}|Bearer [A-Za-z0-9._-]{12,}|v1\.[A-Za-z0-9]{20,}")


@dataclass
class Step:
    name: str
    what: str  # what it measures, and which placeholder or file it feeds
    command: list[str] | None = None  # an `openport` command line, or None for a step run here
    record: tuple[str, str, dict] | None = None  # a dashboard run to record: (endpoint, file in examples/recorded, request)
    keeps: tuple[str, ...] = ()  # what the step overwrites under runs/: set aside first, with the time, never deleted


STEPS = [
    Step("check", "a tiny call to each tier: the key works and the account has credit again", ["smoke"]),
    Step("baseline-supportdesk", "the plain swap on Nano, all 118 cases (VIDEO.md ⟨plain⟩)", ["baseline", "examples/supportdesk", "--tiers", "nano"]),
    Step("tune-supportdesk", "tune SupportDesk's five calls; the report's last line is ⟨own cost⟩", ["tune", "examples/supportdesk"],
         keeps=("plan-supportdesk.json",)),
    Step("port-supportdesk", "port it with the tuned plan", ["port", "examples/supportdesk", "--plan", "runs/plan-supportdesk.json"],
         keeps=("ports/supportdesk", "port-supportdesk.diff")),
    Step("pr-supportdesk", "its PR description: ⟨ported⟩, ⟨before⟩, ⟨after⟩ (runs/pr-supportdesk.md)", ["pr", "runs/ports/supportdesk"],
         keeps=("pr-supportdesk.md",)),
    Step("tune-preflight", "re-tune preflight on its eval pack (119 cases)", ["tune", "examples/evalpacks/preflight"],
         keeps=("plan-preflight.json",)),
    Step("port-preflight", "port the reviewed commit with that plan", ["port", PREFLIGHT, "--plan", "runs/plan-preflight.json"],
         keeps=("ports/jsliapark__preflight", "port-jsliapark__preflight.diff")),
    Step("pr-preflight", "its PR: ⟨preflight before⟩, ⟨preflight after⟩ (runs/pr-jsliapark__preflight.md)", ["pr", "runs/ports/jsliapark__preflight"],
         keeps=("pr-jsliapark__preflight.md",)),
    Step("tune-supportdesk-ts", "tune the TypeScript SupportDesk on the same cases", ["tune", "examples/supportdesk-ts"],
         keeps=("plan-supportdesk-ts.json",)),
    Step("port-supportdesk-ts", "port it with its plan", ["port", "examples/supportdesk-ts", "--plan", "runs/plan-supportdesk-ts.json"],
         keeps=("ports/supportdesk-ts", "port-supportdesk-ts.diff")),
    Step("pr-supportdesk-ts", "its PR description", ["pr", "runs/ports/supportdesk-ts"], keeps=("pr-supportdesk-ts.md",)),
    Step("adapter", f"download the fine-tune's adapter ({FINETUNE_JOB})", ["finetune", "--job", FINETUNE_JOB]),
    Step("record-supportdesk", "the dashboard's SupportDesk demo, live, for its replay",
         record=("/api/runs", "port-supportdesk.json", {"target": "supportdesk", "live": True})),
    Step("record-preflight", "the dashboard's preflight demo, live",
         record=("/api/runs", "port-preflight.json", {"target": "preflight", "live": True})),
    Step("record-authkit", "the AuthKit upgrade, live: its fixes now quote PyJWT's pages (VIDEO.md, 2:00)",
         record=("/api/upgrades", "upgrade-authkit.json", {"target": "authkit", "live": True})),
]


def set_aside(names: tuple[str, ...]) -> list[str]:
    """Keep what a step is about to overwrite: a file is copied, a folder moved, each to a name with the time."""
    kept = []
    stamp = time.strftime("%Y%m%d-%H%M")
    for name in names:
        path = RUNS / name
        if not path.exists():
            continue
        aside = path.with_name(f"{path.stem}.before-{stamp}{path.suffix}" if path.is_file() else f"{path.name}.before-{stamp}")
        if path.is_dir():
            path.rename(aside)  # the port rebuilds the folder from scratch
        else:
            shutil.copy2(path, aside)
        kept.append(aside.relative_to(ROOT).as_posix())
    return kept


def spent_since(started: float) -> float:
    """What the reports written since `started` say this step spent on Token Factory, in USD."""
    total = 0.0
    for path in RUNS.glob("*.json"):
        if path.stat().st_mtime < started:
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if report.get("kind") == "tune":
            total += sum(a.get("cost_usd") or 0 for r in report.get("results", []) for a in r.get("attempts", []))
            total += report.get("helper_cost_usd") or 0
        elif report.get("kind") == "baseline":
            total += sum(r.get("cost_usd") or 0 for r in report.get("results", []))
    for path in (RUNS / "dashboard").glob("*.json") if (RUNS / "dashboard").is_dir() else []:
        if path.stat().st_mtime >= started:
            run = json.loads(path.read_text(encoding="utf-8"))
            total += sum((e.get("evals_usd") or 0) + (e.get("helper_usd") or 0) for e in run.get("events", []) if e.get("type") == "cost")
    return total


def record(endpoint: str, filename: str, request: dict) -> str:
    """Run a dashboard demo live, in this process, and keep its run as the demo's recording."""
    from fastapi.testclient import TestClient

    from openport.server import app

    client = TestClient(app)
    started = client.post(endpoint, json=request)
    started.raise_for_status()
    run_id = started.json()["id"]
    while (run := client.get(f"/api/runs/{run_id}").json())["status"] not in ("done", "error"):
        time.sleep(2)
    if run["status"] != "done":
        raise RuntimeError(f"the run ended with an error: {run['events'][-1] if run['events'] else run}")
    source = RUNS / "dashboard" / f"{run_id}.json"
    text = source.read_text(encoding="utf-8")
    if PRIVATE.search(text):
        raise RuntimeError(f"{source} holds a local path or a key, so it isn't copied; check it by hand")
    destination = RECORDED / filename
    if destination.exists():  # the old recording stays beside it, as git history does
        shutil.copy2(destination, destination.with_suffix(f".before-{time.strftime('%Y%m%d')}.json"))
    shutil.copy2(source, destination)
    return f"recorded {destination.relative_to(ROOT)}; add it to examples/recorded/README.md"


def main(argv: list[str]) -> int:
    steps = STEPS
    if "--from" in argv:
        name = argv[argv.index("--from") + 1]
        steps = STEPS[[s.name for s in STEPS].index(name):]
    budget = float(argv[argv.index("--budget") + 1]) if "--budget" in argv else 10.0
    for step in steps:
        how = f"openport {' '.join(step.command)}" if step.command else f"dashboard {step.record[0]} {json.dumps(step.record[2])}"
        print(f"{step.name:22} {step.what}\n{'':22} {how}")
    if "--run" not in argv:
        print("\nNothing ran. With --run, these spend Token Factory credit (stopping past --budget, now ${:.2f}).".format(budget))
        return 0
    if "--yes" not in argv and input(f"\nRun these {len(steps)} steps on Token Factory credit, up to ${budget:.2f}? [y/N] ").strip().lower() != "y":
        return 1
    total = 0.0
    for step in steps:
        started = time.time()
        print(f"\n== {step.name}: {step.what}", flush=True)
        for aside in set_aside(step.keeps):
            print(f"kept the earlier one as {aside}")
        if step.command:
            failed = subprocess.run([sys.executable, "-X", "utf8", "-m", "openport.cli", *step.command], cwd=ROOT).returncode
            if failed:
                print(f"{step.name} failed; fix it, then resume with --from {step.name}")
                return failed
        else:
            try:
                print(record(*step.record))
            except Exception as error:  # an error stops the sequence where it can be resumed
                print(f"{step.name} failed: {type(error).__name__}: {error}\nResume with --from {step.name}")
                return 1
        total += spent_since(started)
        print(f"Spent so far: ${total:.2f}", flush=True)
        if total > budget:
            later = steps[steps.index(step) + 1:]
            print(f"Past the ${budget:.2f} budget: stopping." + (f" Resume with --from {later[0].name}." if later else ""))
            return 1
    print("\nDone. Fill docs/VIDEO.md's numbers and the deck's placeholders from the reports above.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
