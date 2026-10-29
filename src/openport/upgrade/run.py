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

import shlex
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
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
    changelogs: list = field(default_factory=list)  # a changelog.Digest for each upgraded package whose changelog was found
    research: list[dict] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)  # files a fix added, from the repo's root
    diff: str = ""
    out: str = ""
    fix_error: str = ""  # why Super couldn't be asked for a fix (e.g. the API refused), if it couldn't
    notes: list[str] = field(default_factory=list)  # e.g. a second lockfile with the same versions that couldn't be updated

    @property
    def final(self) -> TestRun | None:
        return self.rounds[-1].tests if self.rounds else self.after_bump

    @property
    def proven(self) -> bool:
        """The tests passed before the upgrade and pass after it."""
        return bool(self.before and self.before.ok and self.final and self.final.ok)


def in_project(project: str, command: str) -> str:
    """A container command run in a monorepo's project folder (the whole repo is mounted, so the project's path
    dependencies on its neighbours still resolve)."""
    return f"cd {shlex.quote(project)} && {command}" if project else command


def upgrade_repo(
    root: Path,
    out: Path,
    *,
    plans: list[Upgrade] | None = None,
    project: str = "",  # in a monorepo: the folder whose lockfiles and tests this upgrade is for, relative to `root`
    min_age_days: int = 7,
    rounds: int = 3,
    run_tests: Callable[[Path], TestRun],
    researcher: Callable[[Upgrade], list[dict]] | None = None,
    changelog: Callable[[Upgrade], object] | None = None,  # an upgrade -> its changelog.Digest, or None
    fixer=None,
    on_event: Callable[[str, object], None] | None = None,
    run_container: Callable[[Path, str, str], TestRun] = run_in_container,  # runs a package manager to bump a lockfile
    release_dates: Callable[[Dependency], dict[str, datetime]] = fetch_releases,  # checks what a bump adds
) -> UpgradeRun:
    emit = on_event or (lambda kind, data: None)
    project = PurePosixPath(project).as_posix().strip("/") if project else ""
    plans = plans if plans is not None else plan_upgrades(read_lockfiles(Path(root) / project), min_age_days=min_age_days)
    upgraded, skipped = [], []
    for p in plans:
        if not p.target:
            skipped.append((p, p.status))
        elif "/" in p.dep.source:
            folder = PurePosixPath(project, p.dep.source).parent.as_posix()
            skipped.append((p, f"locked in {PurePosixPath(project, p.dep.source).as_posix()}: upgrade that project with --project {folder}"))
        elif p.dep.source not in BUMPABLE_SOURCES:
            skipped.append((p, f"{p.dep.source} can't be updated by OpenPort yet: update it with its package manager"))
        elif any(u.dep.source == p.dep.source and u.dep.name.lower() == p.dep.name.lower() for u in upgraded):
            skipped.append((p, f"{p.dep.name} {p.dep.version} is also locked here; this upgrade moves the other one only"))
        else:
            upgraded.append(p)
    name = Path(root).resolve().name
    run = UpgradeRun(f"{name}/{project}" if project else name, upgraded, skipped, None, None, out=str(out))
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
        return _upgrade(root, out, project, scratch_copy, run, tested, min_age_days, rounds, researcher, changelog, fixer, emit,
                        run_container, release_dates)
    finally:
        for folder in set(scratches):
            try:
                remove_tree(folder)
            except OSError:  # harmless: it's never read again, and the next run works around it
                pass


def _upgrade(root, out, project, scratch_copy, run, tested, min_age_days, rounds, researcher, changelog, fixer, emit, run_container,
             release_dates) -> UpgradeRun:
    home = Path(out) / project  # the project's folder in the upgrade: its lockfiles, and the code its tests run

    def repo_path(name: str) -> str:
        return PurePosixPath(project, name).as_posix() if project else name

    run.before = tested()
    emit("before", run.before)
    # Every root lockfile that locks the version moves, not just the one the project's package manager uses
    # (package-lock.json and bun.lock, say), so neither is left vulnerable.
    pinned = {p.dep.name: p.target.version for p in run.upgraded if "requirements.txt" in (p.dep.source, *p.dep.also)}
    run.changed = [repo_path(name) for name in apply_bumps(home, pinned)]
    locked: dict[str, dict[str, str]] = {}
    for p in run.upgraded:
        for source in (p.dep.source, *p.dep.also):
            if source in LOCKFILES:
                locked.setdefault(source, {})[p.dep.name] = p.target.version
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)  # the supply-chain guard, for everything a bump resolves
    for lockfile in [name for name in LOCKFILES if locked.get(name)]:
        step, = bump_steps(home, {lockfile: locked[lockfile]}, cutoff)  # built from the files as the steps before left them
        work = scratch_copy()
        for name, text in step.writes.items():  # e.g. overrides; a failed bump leaves no half-made change behind
            _write(work / project / name, text)
        result = run_container(work, step.image, in_project(project, step.command))
        problem = None if result.ok else result.summary.removeprefix("error: ") or "the package manager failed"
        if problem is None:
            before, after = (_read(f / step.lockfile) if (f / step.lockfile).is_file() else "" for f in (home, work / project))
            young = too_new(added_versions(step.lockfile, before, after), cutoff, release_dates)
            if young:
                more = f" and {len(young) - 3} more" if len(young) > 3 else ""
                problem = f"it would add releases under {min_age_days} days old: {', '.join(young[:3])}{more}"
        if problem is None:
            touched = [name for name in (step.lockfile, *step.files) if (work / project / name).is_file()]
            for name in touched:  # only the files the package manager is there to change come back
                shutil.copy2(work / project / name, home / name)
            run.changed += [repo_path(name) for name in touched]
            continue
        failed = [p for p in run.upgraded if p.dep.source == step.lockfile]
        run.upgraded = [p for p in run.upgraded if p not in failed]
        run.skipped += [(p, f"{step.lockfile} couldn't be updated: {problem}") for p in failed]
        others = [name for name in step.packages if name not in {p.dep.name for p in failed}]
        if others:  # the project's own lockfile moved them; this other one still locks the old versions
            run.notes.append(f"`{repo_path(step.lockfile)}` also locks {', '.join(others)} and couldn't be updated: {problem}")
    emit("bumped", run.upgraded)
    if not run.upgraded:
        return run
    if changelog is not None:  # what changed upstream, for the PR whatever the tests say

        def read(upgrade: Upgrade):
            try:
                return changelog(upgrade)
            except Exception:  # Tavily unreachable or out of credit: the PR goes without it
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            run.changelogs = [found for found in pool.map(read, run.upgraded) if found]
        emit("changelogs", run.changelogs)
    run.after_bump = tested()
    emit("after_bump", run.after_bump)

    current, notes = run.after_bump, ""
    if not current.ok and fixer is not None:
        # The releases in between, in full, come first: Super's quotes from them are checked like any other source's.
        run.research = [{"title": d.title, "url": d.url, "excerpt": d.text} for d in run.changelogs]
        if researcher is not None:
            for upgrade in run.upgraded:
                run.research += researcher(upgrade)
        if researcher is not None or run.research:
            emit("research", run.research)
        summary = [{"name": p.dep.name, "from": p.dep.version, "to": p.target.version} for p in run.upgraded]
        for _ in range(rounds):
            try:  # the tests ran in the project's folder, so the paths in their output start there
                edits, rationale, sources = fixer(summary, run.research, current.output, source_files(home, current.output), notes)
            except Exception as error:  # e.g. Token Factory refusing the request: report it, keep the bump and the tests
                run.fix_error = f"{type(error).__name__}: {error}"[:300]
                break
            applied, rejected = apply_edits(home, edits)
            run.created += [repo_path(e.file) for e in applied if not e.search]
            current = tested() if applied else current
            run.rounds.append(FixRound(applied, rejected, rationale, sources, current))
            emit("round", run.rounds[-1])
            if current.ok:
                break
            notes = f"You changed {len(applied)} snippet(s): {rationale}\n" + "".join(f"- rejected edit to {e.file}: {why}\n" for e, why in rejected)
    run.diff = diff(root, out, run.created)
    return run
