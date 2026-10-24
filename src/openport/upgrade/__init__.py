"""`openport upgrade` (Phase 2): patch vulnerable dependencies and fix what the upgrade breaks, with proof.

Same "prove it, then ship it" engine as the port, aimed at a second kind of PR. Implemented so far:
read lockfiles -> query OSV.dev -> pick a safe target version -> report (dry run). The code fixes,
test runs and PR come next and need a live environment.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .lockfiles import Dependency, read_lockfiles
from .osv import Advisory, find_advisories
from .registry import fetch_releases
from .select import Target, choose_target, fix_floor


@dataclass
class Upgrade:
    dep: Dependency
    advisories: list[Advisory]
    target: Target | None
    status: str  # "upgrade" | "held: fix too new" | "no fix available"

    @property
    def advisory_ids(self) -> list[str]:
        return [a.aliases[0] if a.aliases else a.id for a in self.advisories]


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
    plans: list[Upgrade] = []
    for dep, advisories in find(deps):
        if fix_floor(advisories) is None:
            plans.append(Upgrade(dep, advisories, None, "no fix available"))
            continue
        target = choose_target(dep.version, advisories, releases_for(dep), min_age_days=min_age_days, now=now)
        plans.append(Upgrade(dep, advisories, target, "upgrade" if target else "held: fix too new"))
    return plans


__all__ = [
    "Advisory", "Dependency", "Target", "Upgrade",
    "choose_target", "find_advisories", "fix_floor", "plan_upgrades", "read_lockfiles",
]
