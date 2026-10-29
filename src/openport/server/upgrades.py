"""The dashboard's upgrade pipeline: find vulnerable dependencies, upgrade a copy, prove it with the repo's tests.

It reports each step as an event, like the port pipeline. The tests run in a throwaway Docker container;
where there's no Docker (a hosted endpoint), the run stops after choosing safe versions and says so.
"""

import os
import re
import shutil
from pathlib import Path

from .. import config, tokenfactory
from ..runtime.openport_llm import UsageMeter
from ..upgrade import Upgrade, plan_upgrades, read_lockfiles
from ..upgrade.run import BUMPABLE_SOURCES
from ..workspace import fetch_repo, split_project
from . import pipeline
from .pipeline import PIPELINE_LOCK, Run, _save

PROJECT_FOLDER = re.compile(r"^[\w][\w.-]*(?:/[\w][\w.-]*)*$")  # apps/api: each part a name, none starting with a dot

# A preset's `tests` are the image and command its own CI uses, where the ones OpenPort would pick don't fit: the
# learn-claude-code site has no test suite, and its CI proves it by type-checking and building it.
PRESETS = [
    {"id": "authkit", "label": "AuthKit", "detail": "Demo app: PyJWT 1.7.1 and requests 2.31.0 pinned, with tests", "path": "authkit"},
    {"id": "authkit-node", "label": "AuthKit (Node)", "detail": "The same in Node: jsonwebtoken 8.5.1 in package-lock.json, with tests", "path": "authkit-node"},
    {"id": "learn-claude-code", "label": "shareAI-lab/learn-claude-code", "repo": "shareAI-lab/learn-claude-code/web",
     "detail": "A real repo (79k stars): its Next.js site's vulnerable packages, proven by its own CI's type check and build",
     "tests": {"image": "node:20.20.2-slim",
               "command": "npm ci --no-audit --no-fund && npx tsc --noEmit && npm run build && echo 'CI passed: npm ci, tsc --noEmit, next build'"}},
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
    from ..upgrade.changelog import changelog_digest
    from ..upgrade.fix import make_fixer
    from ..upgrade.research import research_upgrade
    from ..upgrade.run import in_project, upgrade_repo
    from ..upgrade.testrun import run_suites, run_tests, test_setups

    run.emit("stage", stage="fetch")
    repo, project = ("/".join(target.split("/")[:2]), "/".join(target.split("/")[2:])) if local is None else (target, "")
    root = local if local is not None else fetch_repo(repo, max_mb=pipeline.MAX_REPO_MB)[0]
    run.emit("stage", stage="find")
    if project and not (PROJECT_FOLDER.match(project) and (root / project).resolve().is_relative_to(root.resolve())):
        raise ValueError("A project's folder is a path inside its repo, like apps/api")
    if project and not (root / project).is_dir():
        run.emit("note", message=f"{repo} has no folder {project} on its default branch.")
        return
    deps = read_lockfiles(root / project)
    run.emit("deps", count=len(deps), files=sorted({d.source for d in deps}))
    if not deps:
        run.emit("note", message="No lockfiles found (requirements*.txt, uv.lock, poetry.lock, package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock).")
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
    if pipeline.VETTED_ONLY and local is None:  # installing and testing runs the repo's code, even in a container
        run.emit("note", message="Dry run: this public server runs tests only for its demo apps. Run `openport upgrade --apply` "
                                 "locally to prove this upgrade.")
        return

    def event(kind, data):
        if kind in ("before", "after_bump"):
            run.emit("tests", step=kind, **_tests(data))
            if kind == "after_bump" and not data.ok:
                run.emit("stage", stage="fix")  # the research and Super's edits come next
        elif kind == "bumped":  # the packages actually upgraded: a lockfile the package manager couldn't update drops out
            run.emit("bumped", packages=[p.dep.name for p in data], versions=[f"{p.dep.name}@{p.dep.version}" for p in data])
        elif kind == "changelogs":
            run.emit("changelogs", packages=[found.to_dict() for found in data])
        elif kind == "research":
            run.emit("research", sources=[{"title": s["title"], "url": s["url"]} for s in data])
        elif kind == "round":
            run.emit("fix", files=sorted({e.file for e in data.edits}), rationale=data.rationale, sources=data.sources, model=data.model,
                     rejected=[{"file": e.file, "reason": why} for e, why in data.rejected], tests=_tests(data.tests) if data.edits else None)

    run.emit("stage", stage="test")
    researcher = research_upgrade if os.getenv("TAVILY_API_KEY") else None
    changelog = changelog_digest if os.getenv("TAVILY_API_KEY") else None
    ecosystems = {p.dep.ecosystem for p in plans if p.target}
    meter = UsageMeter()  # Super's fixes: what this run spends on Token Factory

    own = next((p["tests"] for p in PRESETS if p.get("repo") == target and p.get("tests")), None)

    def tests(folder: Path):  # a monorepo's project: its suites run in its folder, with the whole repo there
        setups = [("ci", own["image"], own["command"])] if own else test_setups(folder / project, ecosystems)
        return run_suites(folder, [(name, image, in_project(project, command)) for name, image, command in setups], run_tests=run_tests)

    out = config.RUNS_DIR / "upgrades" / (root.resolve().name + (f"-{project.replace('/', '-')}" if project else ""))
    try:
        result = upgrade_repo(root, out, plans=plans, project=project, run_tests=tests, researcher=researcher, changelog=changelog,
                              fixer=make_fixer(meter=meter), on_event=event)
    finally:
        run.emit("cost", evals_usd=0.0, helper_usd=_cost(meter))
    if result.fix_error:
        run.emit("note", message=f"Nemotron Super couldn't be asked for a fix: {result.fix_error}")
    run.emit("stage", stage="pr")
    run.emit("upgrade_pr", body=report.render(result), diff=result.diff[:300_000], proven=result.proven)


def _cost(meter: UsageMeter) -> float:
    total = 0.0
    for model, usage in meter.by_model.items():
        info = tokenfactory.model_info(model)
        total += (info.cost(usage.prompt_tokens, usage.completion_tokens) or 0) if info else 0
    return total


def resolve_target(target: str) -> tuple[str, str, Path | None]:
    """Upgrade presets, a public GitHub repo as owner/name, or a monorepo's project as its folder's GitHub URL
    (https://github.com/owner/name/tree/main/apps/api, kept as owner/name/apps/api)."""
    from .pipeline import resolve_target as resolve

    repo, project = split_project(target)
    if not project:
        return resolve(target, PRESETS)
    # Plain folder names only: no "..", no empty part (a leading "/" would make the folder absolute), no backslash.
    if not PROJECT_FOLDER.match(project):
        raise ValueError("A project's folder is a path inside its repo, like apps/api")
    name, label, _ = resolve(repo, [])
    return f"{name}/{project}", f"{label}/{project}", None
