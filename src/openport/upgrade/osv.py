"""Ask OSV.dev which installed dependencies are vulnerable, and what versions fix them.

OSV.dev is free and needs no key. Each dependency is queried with its exact version, so every
advisory returned applies to what's installed. Response parsing is pure (and tested); the HTTP
call is a thin, injectable wrapper so the rest can be exercised offline.
"""

import re
from dataclasses import dataclass, field

from .lockfiles import Dependency

OSV_QUERY_URL = "https://api.osv.dev/v1/query"


@dataclass
class Advisory:
    id: str
    summary: str
    aliases: list[str] = field(default_factory=list)
    fixed_versions: list[str] = field(default_factory=list)  # versions that fix this advisory for the package
    # Affected ranges for the package, in order: {"introduced", "fixed"?, "last_affected"?}. No "fixed" means no fix yet.
    ranges: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        """One identity for the GHSA and PYSEC records of the same flaw: its CVE when it has one."""
        return next((a for a in [self.id, *self.aliases] if a.startswith("CVE-")), self.id)


def osv_name(dep: Dependency) -> str:
    """The package name as OSV indexes it: PyPI names are normalized (lowercase, runs of -_. to -)."""
    if dep.ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", dep.name).lower()
    return dep.name


def query_body(dep: Dependency) -> dict:
    return {"version": dep.version, "package": {"name": osv_name(dep), "ecosystem": dep.ecosystem}}


def parse_vuln(vuln: dict, ecosystem: str, name: str) -> Advisory:
    """Pull the fixed versions for one package out of a single OSV vulnerability record."""
    fixed: list[str] = []
    ranges: list[dict] = []
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("ecosystem") != ecosystem or pkg.get("name") != name:
            continue
        for rng in affected.get("ranges", []):
            if rng.get("type") == "GIT":
                continue  # commit hashes, not package versions
            introduced = None
            for event in rng.get("events", []):
                if "introduced" in event:
                    if introduced is not None:
                        ranges.append({"introduced": introduced})  # an earlier range that never closed
                    introduced = event["introduced"]
                elif event.get("fixed") or event.get("last_affected"):
                    end = "fixed" if event.get("fixed") else "last_affected"
                    ranges.append({"introduced": introduced or "0", end: event[end]})
                    if end == "fixed":
                        fixed.append(event["fixed"])
                    introduced = None
            if introduced is not None:
                ranges.append({"introduced": introduced})
    summary = vuln.get("summary") or (vuln.get("details", "") or "")[:200]
    return Advisory(
        id=vuln.get("id", ""),
        summary=summary,
        aliases=list(vuln.get("aliases", [])),
        fixed_versions=sorted(set(fixed)),
        ranges=ranges,
    )


def dedupe(advisories: list[Advisory]) -> list[Advisory]:
    """OSV returns a flaw once per database (GHSA, PYSEC). Keep one record per CVE, preferring GHSA."""
    chosen: dict[str, Advisory] = {}
    for advisory in sorted(advisories, key=lambda a: not a.id.startswith("GHSA-")):
        chosen.setdefault(advisory.key, advisory)
    return list(chosen.values())


def _http_fetch(body: dict) -> list[dict]:
    import httpx

    response = httpx.post(OSV_QUERY_URL, json=body, timeout=30)
    response.raise_for_status()
    return response.json().get("vulns", []) or []


def find_advisories(deps: list[Dependency], fetch=None) -> list[tuple[Dependency, list[Advisory]]]:
    """For each vulnerable dependency, the advisories against the installed version. `fetch` is injectable."""
    fetch = fetch or _http_fetch
    results: list[tuple[Dependency, list[Advisory]]] = []
    for dep in deps:
        name = osv_name(dep)
        advisories = dedupe([a for a in (parse_vuln(vuln, dep.ecosystem, name) for vuln in fetch(query_body(dep))) if a.id])
        if advisories:
            results.append((dep, advisories))
    return results
