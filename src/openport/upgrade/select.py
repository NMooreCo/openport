"""Choose a safe upgrade target: the earliest release that fixes the advisories and isn't brand new.

Recent supply-chain attacks shipped malicious *new* releases, so OpenPort never jumps onto a version that
landed in the last few days — it takes the lowest version that fixes every advisory and is at least
`min_age_days` old. Prereleases are skipped. Selection is pure; release dates come from the registry.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from packaging.version import InvalidVersion, Version

from .osv import Advisory


@dataclass
class Target:
    version: str
    released: datetime
    age_days: int


def _parse(value: str) -> Version | None:
    try:
        return Version(value)
    except InvalidVersion:
        return None


def fix_floor(advisories: list[Advisory]) -> Version | None:
    """The lowest version that resolves *every* advisory, or None if any advisory has no known fix."""
    floors: list[Version] = []
    for advisory in advisories:
        fixes = [v for v in (_parse(f) for f in advisory.fixed_versions) if v is not None]
        if not fixes:
            return None  # an advisory with no fixed version: no safe upgrade can be promised
        floors.append(min(fixes))
    return max(floors) if floors else None


def choose_target(
    current: str,
    advisories: list[Advisory],
    releases: dict[str, datetime],
    *,
    min_age_days: int = 7,
    now: datetime | None = None,
) -> Target | None:
    """Smallest stable release that is >= the fix floor, newer than `current`, and at least min_age_days old."""
    floor = fix_floor(advisories)
    if floor is None:
        return None
    now = now or datetime.now(timezone.utc)
    current_version = _parse(current)

    best: tuple[Version, str, datetime, int] | None = None
    for version_str, released in releases.items():
        version = _parse(version_str)
        if version is None or version.is_prerelease or released is None:
            continue
        if version < floor:
            continue
        if current_version is not None and version <= current_version:
            continue
        age_days = (now - released).days
        if age_days < min_age_days:
            continue  # too new to trust: the supply-chain guard
        if best is None or version < best[0]:
            best = (version, version_str, released, age_days)

    if best is None:
        return None
    return Target(best[1], best[2], best[3])
