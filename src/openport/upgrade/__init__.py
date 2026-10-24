"""`openport upgrade` (Phase 2): patch vulnerable dependencies and fix what the upgrade breaks, with proof.

Same "prove it, then ship it" engine as the port, aimed at a second kind of PR: read lockfiles ->
query OSV.dev -> pick a safe target version (this module) -> with --apply, bump a copy, run its tests
in a container and have Nemotron Super fix what broke (run.py) -> the PR description (report.py).
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .lockfiles import Dependency, read_lockfiles
from .osv import Advisory, find_advisories
from .registry import fetch_releases
from .select import Target, choose_target, fix_floor, has_fix


@dataclass
class Upgrade:
    dep: Dependency
    advisories: list[Advisory]
    target: Target | None
    status: str  # "upgrade" | "partial" (some advisories stay open) | "held: fix too new" | "held: no release dates" | "no fix available"

    @property
    def advisory_ids(self) -> list[str]:
        return list(dict.fromkeys(a.key for a in self.advisories))


def plan_upgrades(
    deps: list[Dependency],
    *,
    find=None,
    releases_for=None,
    min_age_days: int = 7,
    now: datetime | None = None,
) -> list[Upgrade]:
    """For each vulnerable dependency, the safe target version (or why none was chosen). Injectable for tests."""
    find = find or find_advisories
    releases_for = releases_for or fetch_releases
    found = find(deps)
    fixable = [dep for dep, advisories in found if any(has_fix(a) for a in advisories)]

    def dates(dep: Dependency) -> dict[str, datetime] | None:
        try:
            return releases_for(dep)
        except Exception:  # e.g. a private registry: without dates, no version can be shown to be old enough
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        releases = dict(zip(fixable, pool.map(dates, fixable)))
    plans: list[Upgrade] = []
    for dep, advisories in found:
        if dep not in releases:
            plans.append(Upgrade(dep, advisories, None, "no fix available"))
        elif releases[dep] is None:
            plans.append(Upgrade(dep, advisories, None, "held: no release dates"))
        else:
            target = choose_target(dep.version, advisories, releases[dep], min_age_days=min_age_days, now=now)
            status = "held: fix too new" if target is None else "partial" if target.still_open else "upgrade"
            plans.append(Upgrade(dep, advisories, target, status))
    return plans


__all__ = [
    "Advisory", "Dependency", "Target", "Upgrade",
    "choose_target", "find_advisories", "fix_floor", "plan_upgrades", "read_lockfiles",
]
