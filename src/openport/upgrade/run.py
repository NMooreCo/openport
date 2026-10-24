"""`openport upgrade --apply`: bump vulnerable pins in a copy of the repo, prove it with the repo's own tests,
and have Nemotron Super repair whatever the upgrade broke.

1. Run the tests on a copy of the repo as it is (the baseline must pass for the upgrade to be provable).
2. Bump each fixable dependency to its chosen version and run the tests again.
3. If they fail: Tavily reads the changelogs and advisories, Super proposes exact edits to application
   code (never tests), and the tests run again, for a few rounds.
4. Diff the copy against the original. The original repo is never touched.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import Upgrade, plan_upgrades, read_lockfiles
from .apply import Edit, apply_bumps, apply_edits, copy_repo, diff
from .fix import source_files
from .testrun import TestRun

BUMPABLE_SOURCES = ("requirements.txt",)  # pinned requirements; lockfile-managed deps need their package manager


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
) -> UpgradeRun:
    emit = on_event or (lambda kind, data: None)
    plans = plans if plans is not None else plan_upgrades(read_lockfiles(root), min_age_days=min_age_days)
    upgraded = [p for p in plans if p.target and p.dep.source in BUMPABLE_SOURCES]
    skipped = [(p, p.status if not p.target else f"pinned in {p.dep.source}; update it with its package manager")
               for p in plans if p not in upgraded]
    run = UpgradeRun(Path(root).resolve().name, upgraded, skipped, None, None, out=str(out))
    if not upgraded:
        return run

    copy_repo(root, out)
    run.before = run_tests(out)
    emit("before", run.before)
    run.changed = apply_bumps(out, {p.dep.name: p.target.version for p in upgraded})
    run.after_bump = run_tests(out)
    emit("after_bump", run.after_bump)

    current, notes = run.after_bump, ""
    if not current.ok and fixer is not None:
        if researcher is not None:
            for upgrade in upgraded:
                run.research += researcher(upgrade)
            emit("research", run.research)
        summary = [{"name": p.dep.name, "from": p.dep.version, "to": p.target.version} for p in upgraded]
        for _ in range(rounds):
            edits, rationale, sources = fixer(summary, run.research, current.output, source_files(out, current.output), notes)
            applied, rejected = apply_edits(out, edits)
            current = run_tests(out) if applied else current
            run.rounds.append(FixRound(applied, rejected, rationale, sources, current))
            emit("round", run.rounds[-1])
            if current.ok:
                break
            notes = f"You changed {len(applied)} snippet(s): {rationale}\n" + "".join(f"- rejected edit to {e.file}: {why}\n" for e, why in rejected)
    run.diff = diff(root, out)
    return run
