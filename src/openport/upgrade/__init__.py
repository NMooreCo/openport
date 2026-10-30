"""`openport upgrade` (Phase 2): patch vulnerable dependencies and fix what the upgrade breaks, with proof.

Same "prove it, then ship it" engine as the port, aimed at a second kind of PR: read lockfiles ->
query OSV.dev -> pick a safe target version (this module) -> with --apply, bump a copy, run its tests
in a container and have Nemotron Super fix what broke (run.py) -> the PR description (report.py).
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime

from .lockfiles import Dependency, read_lockfiles
from .osv import Advisory, find_advisories
from .registry import fetch_release_dates, fetch_releases
from .select import Target, choose_target, fix_floor, has_fix, parse_version


@dataclass
class Upgrade:
    dep: Dependency
    advisories: list[Advisory]
    target: Target | None
    # "upgrade" | "partial" (some advisories stay open) | "held: fix too new" | "held: no release dates"
    # | "held: installed version unreadable" | "held: only deprecated releases, none fixing it" | "no fix available"
    status: str

    @property
    def advisory_ids(self) -> list[str]:
        return list(dict.fromkeys(a.key for a in self.advisories))


def plan_upgrades(
    deps: list[Dependency],
    *,
    find=None,
    releases_for=None,
    deprecated_for=None,  # an npm package's releases, deprecated ones too: where a fix goes when they're all there is
    min_age_days: int = 7,
    now: datetime | None = None,
) -> list[Upgrade]:
    """For each vulnerable dependency, the safe target version (or why none was chosen). Injectable for tests."""
    find = find or find_advisories
    if releases_for is None:  # the registries themselves
        releases_for, deprecated_for = fetch_releases, deprecated_for or fetch_release_dates
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
        elif parse_version(dep.version, dep.ecosystem) is None:  # no release can be shown to be newer than it
            plans.append(Upgrade(dep, advisories, None, "held: installed version unreadable"))
        else:
            def choose(releases_of: dict[str, datetime], age: int = min_age_days) -> Target | None:
                return choose_target(dep.version, advisories, releases_of, min_age_days=age, now=now, ecosystem=dep.ecosystem)

            target, status = choose(releases[dep]), None
            if target is None and deprecated_for is not None and dep.ecosystem == "npm":
                # A fix isn't moved to a deprecated release when another will do. When none will (node-sass's 148
                # releases are all deprecated), the deprecated release that fixes it is still the fix.
                try:
                    every = deprecated_for(dep) or {}
                except Exception:  # noqa: BLE001 (the registry, again: no dates, no fix shown to be old enough)
                    every = {}
                target = choose(every) if every else None
                if target is not None:
                    target = replace(target, deprecated=True)
                elif every and not releases[dep] and choose(every, 0) is None:
                    status = "held: only deprecated releases, none fixing it"
            status = status or ("held: fix too new" if target is None else "partial" if target.still_open else "upgrade")
            plans.append(Upgrade(dep, advisories, target, status))
    return plans


__all__ = [
    "Advisory", "Dependency", "Target", "Upgrade",
    "choose_target", "find_advisories", "fix_floor", "plan_upgrades", "read_lockfiles",
]
