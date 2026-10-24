"""Choose a safe upgrade target: the release that fixes the most advisories without being brand new.

Recent supply-chain attacks shipped malicious *new* releases, so OpenPort never jumps onto a version that
landed in the last few days. Among stable releases at least `min_age_days` old and newer than what's
installed, it takes the one that fixes the most advisories (the smallest such version on a tie). An
advisory with no fix yet, or whose fix is too new, doesn't block fixing the others: it's reported as
still open. Whether a version is affected follows OSV's ranges, so a fix on another release branch
doesn't count. Selection is pure; release dates come from the registry.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from packaging.version import InvalidVersion, Version

from .osv import Advisory


@dataclass
class Target:
    version: str
    released: datetime
    age_days: int
    fixed: list[str] = field(default_factory=list)  # advisory IDs this version fixes
    still_open: list[str] = field(default_factory=list)  # no fix yet, or only in a release too new to trust


def _parse(value: str | None) -> Version | None:
    try:
        return Version(value) if value is not None else None
    except InvalidVersion:
        return None


def _ranges(advisory: Advisory) -> list[dict]:
    if advisory.ranges:
        return advisory.ranges
    fixes = [v for v in map(_parse, advisory.fixed_versions) if v is not None]
    return [{"introduced": "0", "fixed": str(min(fixes))}] if fixes else [{"introduced": "0"}]


def affected(version: Version, advisory: Advisory) -> bool:
    for rng in _ranges(advisory):
        low = Version("0") if rng.get("introduced") in (None, "0") else _parse(rng["introduced"])
        if low is None or version < low:
            continue
        if "fixed" in rng:
            high = _parse(rng["fixed"])
            if high is None or version < high:
                return True
        elif "last_affected" in rng:
            last = _parse(rng["last_affected"])
            if last is None or version <= last:
                return True
        else:
            return True  # open-ended: no fix on this branch yet
    return False


def has_fix(advisory: Advisory) -> bool:
    return any("fixed" in r for r in _ranges(advisory))


def fix_floor(advisories: list[Advisory]) -> Version | None:
    """The lowest version that resolves every advisory that has a fix, or None if none of them do."""
    floors = [min(v for v in map(_parse, a.fixed_versions) if v is not None) for a in advisories
              if any(_parse(f) is not None for f in a.fixed_versions)]
    return max(floors) if floors else None


def choose_target(
    current: str,
    advisories: list[Advisory],
    releases: dict[str, datetime],
    *,
    min_age_days: int = 7,
    now: datetime | None = None,
) -> Target | None:
    """The stable, old-enough release newer than `current` that fixes the most advisories; None if none fixes any."""
    now = now or datetime.now(timezone.utc)
    current_version = _parse(current)
    best: tuple[int, Version, str, datetime, list[str]] | None = None
    for version_str, released in releases.items():
        version = _parse(version_str)
        if version is None or version.is_prerelease or released is None:
            continue
        if current_version is not None and version <= current_version:
            continue
        if (now - released).days < min_age_days:
            continue  # too new to trust: the supply-chain guard
        fixed = [a.key for a in advisories if not affected(version, a)]
        if not fixed:
            continue
        if best is None or len(fixed) > best[0] or (len(fixed) == best[0] and version < best[1]):
            best = (len(fixed), version, version_str, released, fixed)
    if best is None:
        return None
    _, _, version_str, released, fixed = best
    still_open = [a.key for a in advisories if a.key not in fixed]
    return Target(version_str, released, (now - released).days, fixed, still_open)
