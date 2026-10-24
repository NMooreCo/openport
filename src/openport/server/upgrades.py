"""The dashboard's upgrade pipeline: find vulnerable dependencies, upgrade a copy, prove it with the repo's tests.

It reports each step as an event, like the port pipeline. The tests run in a throwaway Docker container;
where there's no Docker (a hosted endpoint), the run stops after choosing safe versions and says so.
"""

import os
import shutil
from pathlib import Path

from .. import config
from ..upgrade import Upgrade, plan_upgrades, read_lockfiles
from ..upgrade.run import BUMPABLE_SOURCES
from ..workspace import fetch_repo
from .pipeline import PIPELINE_LOCK, Run, _save

PRESETS = [
    {"id": "authkit", "label": "AuthKit", "detail": "Demo app: PyJWT 1.7.1 and requests 2.31.0 pinned, with tests", "path": "authkit"},
    {"id": "authkit-node", "label": "AuthKit (Node)", "detail": "The same in Node: jsonwebtoken 8.5.1 in package-lock.json, with tests", "path": "authkit-node"},
]
APPLY = os.getenv("OPENPORT_UPGRADE_APPLY", "1") != "0"  # set 0 to only choose versions


def _package(up: Upgrade) -> dict:
    t = up.target
    return {
        "name": up.dep.name, "version": up.dep.version, "ecosystem": up.dep.ecosystem, "source": up.dep.source, "status": up.status,
        "target": t.version if t else None, "age_days": t.age_days if t else None,
        "fixed": t.fixed if t else [], "still_open": t.still_open if t else up.advisory_ids,
        "advisories": [{"id": a.key, "summary": a.summary} for a in up.advisories],
        "bumpable": up.dep.source in BUMPABLE_SOURCES,  # lockfile pins need their package manager
    }


def _tests(result) -> dict:
    return {"ok": result.ok, "summary": result.summary, "seconds": result.seconds, "output": result.output[-3000:]}


def run_pipeline(run: Run, target: str, local: Path | None) -> None:
    with PIPELINE_LOCK:  # one job at a time, ports and upgrades alike
        run.status = "running"
        try:
            _upgrade(run, target, local)
            run.status = "done"
            run.emit("done")
        except Exception as error:  # surface every failure to the dashboard
            run.status = "error"
            run.emit("failed", message=f"{type(error).__name__}: {error}")
        _save(run)


def _upgrade(run: Run, target: str, local: Path | None) -> None:
    from ..upgrade import report
    from ..upgrade.fix import make_fixer
    from ..upgrade.research import research_upgrade
    from ..upgrade.run import upgrade_repo
    from ..upgrade.testrun import run_tests, test_setup

    run.emit("stage", stage="fetch")
    root = local if local is not None else fetch_repo(target)[0]
    run.emit("stage", stage="find")
    deps = read_lockfiles(root)
    run.emit("deps", count=len(deps), files=sorted({d.source for d in deps}))
    if not deps:
        run.emit("note", message="No lockfiles found (requirements*.txt, uv.lock, poetry.lock, package-lock.json, pnpm-lock.yaml).")
        return
    plans = plan_upgrades(deps)
    run.emit("advisories", packages=[_package(p) for p in plans])
    if not any(p.target for p in plans):
        run.emit("note", message="No dependency has a known vulnerability with a safe fix yet." if plans else "No known vulnerabilities.")
        return
    if not APPLY or shutil.which("docker") is None:
        run.emit("note", message="Dry run: proving an upgrade means running the repo's tests in a container, which this server "
                                 "can't do. Run `openport upgrade --apply` locally.")
        return

    def event(kind, data):
        if kind in ("before", "after_bump"):
            run.emit("tests", step=kind, **_tests(data))
            if kind == "after_bump" and not data.ok:
                run.emit("stage", stage="fix")  # the research and Super's edits come next
        elif kind == "research":
            run.emit("research", sources=[{"title": s["title"], "url": s["url"]} for s in data])
        elif kind == "round":
            run.emit("fix", files=sorted({e.file for e in data.edits}), rationale=data.rationale, sources=data.sources,
                     rejected=[{"file": e.file, "reason": why} for e, why in data.rejected], tests=_tests(data.tests) if data.edits else None)

    run.emit("stage", stage="test")
    researcher = research_upgrade if os.getenv("TAVILY_API_KEY") else None
    ecosystems = {p.dep.ecosystem for p in plans if p.target}

    def tests(folder: Path):
        image, command = test_setup(folder, ecosystems)
        return run_tests(folder, image=image, command=command)

    result = upgrade_repo(root, config.RUNS_DIR / "upgrades" / root.resolve().name, plans=plans, run_tests=tests,
                          researcher=researcher, fixer=make_fixer(), on_event=event)
    run.emit("stage", stage="pr")
    run.emit("upgrade_pr", body=report.render(result), diff=result.diff[:300_000], proven=result.proven)


def resolve_target(target: str) -> tuple[str, str, Path | None]:
    """Upgrade presets, or a public GitHub repo as owner/name."""
    from .pipeline import resolve_target as resolve

    return resolve(target, PRESETS)
