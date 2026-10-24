"""`openport upgrade --apply`: bump vulnerable pins in a copy of the repo, prove it with the repo's own tests,
and have Nemotron Super repair whatever the upgrade broke.

1. Run the tests on a copy of the repo as it is (the baseline must pass for the upgrade to be provable).
2. Bump each fixable dependency to its chosen version and run the tests again.
3. If they fail: Tavily reads the changelogs and advisories, Super proposes exact edits to application
   code (never tests), and the tests run again, for a few rounds.
4. Diff the copy against the original. The original repo is never touched.

The upgrade lives in `out`, which only OpenPort's own edits and the package managers' lockfiles change.
The tests and package managers run on a fresh scratch copy each time, so nothing the repo's code does
in its container can reach the diff or the PR.
"""

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import Upgrade, plan_upgrades, read_lockfiles
from .apply import Edit, _read, _write, apply_bumps, apply_edits, copy_repo, diff, remove_tree
from .bump import LOCKFILES, added_versions, bump_steps, too_new
from .fix import source_files
from .lockfiles import Dependency
from .registry import fetch_releases
from .testrun import TestRun, run_in_container

# Pinned requirements are edited in place; the lockfiles are rewritten by their own package managers (bump.py).
BUMPABLE_SOURCES = ("requirements.txt", *LOCKFILES)


@dataclass
class FixRound:
    edits: list[Edit]
    rejected: list[tuple[Edit, str]]
    rationale: str
    sources: list[dict]  # pages the fix followed, each with the sentence it quoted: {"title", "url", "quote"}
    tests: TestRun


@dataclass
class UpgradeRun:
    target: str
    upgraded: list[Upgrade]
    skipped: list[tuple[Upgrade, str]]
    before: TestRun | None
    after_bump: TestRun | None
    rounds: list[FixRound] = field(default_factory=list)
    research: list[dict] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    diff: str = ""
    out: str = ""
    fix_error: str = ""  # why Super couldn't be asked for a fix (e.g. the API refused), if it couldn't

    @property
    def final(self) -> TestRun | None:
        return self.rounds[-1].tests if self.rounds else self.after_bump

    @property
    def proven(self) -> bool:
        """The tests passed before the upgrade and pass after it."""
        return bool(self.before and self.before.ok and self.final and self.final.ok)


def upgrade_repo(
    root: Path,
    out: Path,
    *,
    plans: list[Upgrade] | None = None,
    min_age_days: int = 7,
    rounds: int = 3,
    run_tests: Callable[[Path], TestRun],
    researcher: Callable[[Upgrade], list[dict]] | None = None,
    fixer=None,
    on_event: Callable[[str, object], None] | None = None,
    run_container: Callable[[Path, str, str], TestRun] = run_in_container,  # runs a package manager to bump a lockfile
    release_dates: Callable[[Dependency], dict[str, datetime]] = fetch_releases,  # checks what a bump adds
) -> UpgradeRun:
    emit = on_event or (lambda kind, data: None)
    plans = plans if plans is not None else plan_upgrades(read_lockfiles(root), min_age_days=min_age_days)
    upgraded, skipped = [], []
    for p in plans:
        if not p.target:
            skipped.append((p, p.status))
        elif "/" in p.dep.source:
            skipped.append((p, f"locked in {p.dep.source}: run the upgrade on that folder"))
        elif p.dep.source not in BUMPABLE_SOURCES:
            skipped.append((p, f"{p.dep.source} can't be updated by OpenPort yet: update it with its package manager"))
        elif any(u.dep.source == p.dep.source and u.dep.name.lower() == p.dep.name.lower() for u in upgraded):
            skipped.append((p, f"{p.dep.name} {p.dep.version} is also locked here; this upgrade moves the other one only"))
        else:
            upgraded.append(p)
    run = UpgradeRun(Path(root).resolve().name, upgraded, skipped, None, None, out=str(out))
    if not upgraded:
        return run

    copy_repo(root, out)
    scratches: list[Path] = []

    def scratch_copy() -> Path:
        """A fresh copy of the upgrade for a container to work in. If an earlier copy can't be deleted (a
        container left something this OS can't remove), the next one gets a new folder instead of failing."""
        base = Path(out).with_name(Path(out).name + ".scratch")
        for folder in (base, *(base.with_name(f"{base.name}-{i}") for i in range(1, 100))):
            try:
                copy_repo(out, folder)
            except OSError:
                continue
            scratches.append(folder)
            return folder
        raise OSError(f"couldn't make a scratch copy next to {out}")

    def tested() -> TestRun:
        return run_tests(scratch_copy())  # never `out` itself: the tests run the repo's code

    try:
        return _upgrade(root, out, scratch_copy, run, tested, min_age_days, rounds, researcher, fixer, emit, run_container, release_dates)
    finally:
        for folder in set(scratches):
            try:
                remove_tree(folder)
            except OSError:  # harmless: it's never read again, and the next run works around it
                pass


def _upgrade(root, out, scratch_copy, run, tested, min_age_days, rounds, researcher, fixer, emit, run_container, release_dates) -> UpgradeRun:
    run.before = tested()
    emit("before", run.before)
    run.changed = apply_bumps(out, {p.dep.name: p.target.version for p in run.upgraded if p.dep.source == "requirements.txt"})
    locked: dict[str, dict[str, str]] = {}
    for p in run.upgraded:
        if p.dep.source in LOCKFILES:
            locked.setdefault(p.dep.source, {})[p.dep.name] = p.target.version
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)  # the supply-chain guard, for everything a bump resolves
    for step in bump_steps(out, locked, cutoff):
        work = scratch_copy()
        for name, text in step.writes.items():  # e.g. overrides; a failed bump leaves no half-made change behind
            _write(work / name, text)
        result = run_container(work, step.image, step.command)
        problem = None if result.ok else result.summary.removeprefix("error: ") or "the package manager failed"
        if problem is None:
            before, after = (_read(f / step.lockfile) if (f / step.lockfile).is_file() else "" for f in (Path(out), work))
            young = too_new(added_versions(step.lockfile, before, after), cutoff, release_dates)
            if young:
                more = f" and {len(young) - 3} more" if len(young) > 3 else ""
                problem = f"it would add releases under {min_age_days} days old: {', '.join(young[:3])}{more}"
        if problem is None:
            touched = [name for name in (step.lockfile, *step.files) if (work / name).is_file()]
            for name in touched:  # only the files the package manager is there to change come back
                shutil.copy2(work / name, Path(out) / name)
            run.changed += touched
            continue
        failed = [p for p in run.upgraded if p.dep.source == step.lockfile]
        run.upgraded = [p for p in run.upgraded if p not in failed]
        run.skipped += [(p, f"{step.lockfile} couldn't be updated: {problem}") for p in failed]
    emit("bumped", run.upgraded)
    if not run.upgraded:
        return run
    run.after_bump = tested()
    emit("after_bump", run.after_bump)

    current, notes = run.after_bump, ""
    if not current.ok and fixer is not None:
        if researcher is not None:
            for upgrade in run.upgraded:
                run.research += researcher(upgrade)
            emit("research", run.research)
        summary = [{"name": p.dep.name, "from": p.dep.version, "to": p.target.version} for p in run.upgraded]
        for _ in range(rounds):
            try:
                edits, rationale, sources = fixer(summary, run.research, current.output, source_files(out, current.output), notes)
            except Exception as error:  # e.g. Token Factory refusing the request: report it, keep the bump and the tests
                run.fix_error = f"{type(error).__name__}: {error}"[:300]
                break
            applied, rejected = apply_edits(out, edits)
            current = tested() if applied else current
            run.rounds.append(FixRound(applied, rejected, rationale, sources, current))
            emit("round", run.rounds[-1])
            if current.ok:
                break
            notes = f"You changed {len(applied)} snippet(s): {rationale}\n" + "".join(f"- rejected edit to {e.file}: {why}\n" for e, why in rejected)
    run.diff = diff(root, out)
    return run
