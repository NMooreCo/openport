"""Choose a safe upgrade target: the release that fixes the most advisories without being brand new.

Recent supply-chain attacks shipped malicious *new* releases, so OpenPort never jumps onto a version that
landed in the last few days. Among stable releases at least `min_age_days` old and newer than what's
installed, it takes the one that fixes the most advisories (the smallest such version on a tie). An
advisory with no fix yet, or whose fix is too new, doesn't block fixing the others: it's reported as
still open. Whether a version is affected follows OSV's ranges, so a fix on another release branch
doesn't count. Versions are ordered as their ecosystem orders them: PEP 440 for PyPI, semver for npm.
Selection is pure; release dates come from the registry.
"""

import functools
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packaging.version import InvalidVersion, Version

from .osv import Advisory

_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?")


@functools.total_ordering
class SemVer:
    """An npm version, ordered as semver orders it. PEP 440 would read 2.0.1-0 as a post-release of 2.0.1 (newer, and
    stable) and couldn't read 15.0.0-canary.0 at all; in semver both are prereleases, below their release."""

    def __init__(self, text: str, match: re.Match):
        self.text = text
        self.pre = match[4]
        identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in (self.pre or "").split(".") if part)
        self.key = (int(match[1]), int(match[2]), int(match[3]), 0 if self.pre else 1, identifiers)

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None

    def __eq__(self, other) -> bool:
        return isinstance(other, SemVer) and self.key == other.key

    def __lt__(self, other: "SemVer") -> bool:
        return self.key < other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return self.text


@dataclass
class Target:
    version: str
    released: datetime
    age_days: int
    fixed: list[str] = field(default_factory=list)  # advisory IDs this version fixes
    still_open: list[str] = field(default_factory=list)  # no fix yet, or only in a release too new to trust
    deprecated: bool = False  # every release that fixes it is deprecated (node-sass's all are): still the fix


def parse_version(value: str | None, ecosystem: str = "PyPI") -> Version | SemVer | None:
    """A version as its ecosystem orders it, or None if it can't be read."""
    if value is None:
        return None
    if ecosystem == "npm":
        match = _SEMVER.fullmatch(value.strip())
        return SemVer(value, match) if match else None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _ranges(advisory: Advisory, ecosystem: str = "PyPI") -> list[dict]:
    if advisory.ranges:
        return advisory.ranges
    fixes = [v for v in (parse_version(f, ecosystem) for f in advisory.fixed_versions) if v is not None]
    return [{"introduced": "0", "fixed": str(min(fixes))}] if fixes else [{"introduced": "0"}]


def affected(version, advisory: Advisory, ecosystem: str = "PyPI") -> bool:
    for rng in _ranges(advisory, ecosystem):
        introduced = rng.get("introduced")
        low = None if introduced in (None, "0") else parse_version(introduced, ecosystem)
        if low is not None and version < low:
            continue  # before this range; one whose start can't be read counts from the beginning, to be safe
        if "fixed" in rng:
            high = parse_version(rng["fixed"], ecosystem)
            if high is None or version < high:
                return True
        elif "last_affected" in rng:
            last = parse_version(rng["last_affected"], ecosystem)
            if last is None or version <= last:
                return True
        else:
            return True  # open-ended: no fix on this branch yet
    return False


def has_fix(advisory: Advisory) -> bool:
    return any("fixed" in r for r in _ranges(advisory))


def fix_floor(advisories: list[Advisory], ecosystem: str = "PyPI") -> Version | SemVer | None:
    """The lowest version that resolves every advisory that has a fix, or None if none of them do."""
    floors = [min(v for v in (parse_version(f, ecosystem) for f in a.fixed_versions) if v is not None) for a in advisories
              if any(parse_version(f, ecosystem) is not None for f in a.fixed_versions)]
    return max(floors) if floors else None


def choose_target(
    current: str,
    advisories: list[Advisory],
    releases: dict[str, datetime],
    *,
    min_age_days: int = 7,
    now: datetime | None = None,
    ecosystem: str = "PyPI",
) -> Target | None:
    """The stable, old-enough release newer than `current` that fixes the most advisories; None if none fixes any, or
    if `current` can't be read (then no release can be shown to be newer, and a pick could be a downgrade)."""
    now = now or datetime.now(timezone.utc)
    current_version = parse_version(current, ecosystem)
    if current_version is None:
        return None
    best: tuple[int, Version | SemVer, str, datetime, list[str]] | None = None
    for version_str, released in releases.items():
        version = parse_version(version_str, ecosystem)
        if version is None or version.is_prerelease or released is None:
            continue
        if version <= current_version:
            continue
        if (now - released).days < min_age_days:
            continue  # too new to trust: the supply-chain guard
        fixed = [a.key for a in advisories if not affected(version, a, ecosystem)]
        if not fixed:
            continue
        if best is None or len(fixed) > best[0] or (len(fixed) == best[0] and version < best[1]):
            best = (len(fixed), version, version_str, released, fixed)
    if best is None:
        return None
    _, _, version_str, released, fixed = best
    still_open = [a.key for a in advisories if a.key not in fixed]
    return Target(version_str, released, (now - released).days, fixed, still_open)
