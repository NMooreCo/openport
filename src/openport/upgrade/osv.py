"""Ask OSV.dev which installed dependencies are vulnerable, and what versions fix them.

OSV.dev is free and needs no key. Each dependency is queried with its exact version, so every
advisory returned applies to what's installed. The batch endpoint names the vulnerabilities for up to
1,000 dependencies per request (a Node app's lockfile often lists thousands), and each distinct record
is then fetched once. Response parsing is pure (and tested); the HTTP calls are thin, injectable wrappers
so the rest can be exercised offline.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .lockfiles import Dependency

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{id}"
BATCH = 1000  # OSV's limit per batch request


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


def _http_batch(bodies: list[dict]) -> list[list[str]]:
    """The vulnerability IDs for each query, in order."""
    import httpx

    found: list[list[str]] = []
    for start in range(0, len(bodies), BATCH):
        chunk = bodies[start : start + BATCH]
        response = httpx.post(OSV_BATCH_URL, json={"queries": chunk}, timeout=60)
        response.raise_for_status()
        for body, result in zip(chunk, response.json().get("results", [])):
            if result.get("next_page_token"):  # more than a page for one version: ask for that one in full
                found.append([vuln["id"] for vuln in _http_query(body)])
            else:
                found.append([vuln["id"] for vuln in result.get("vulns") or []])
    return found


def _http_query(body: dict) -> list[dict]:
    import httpx

    vulns, token = [], None
    while True:
        response = httpx.post(OSV_QUERY_URL, json={**body, **({"page_token": token} if token else {})}, timeout=30)
        response.raise_for_status()
        data = response.json()
        vulns += data.get("vulns", []) or []
        token = data.get("next_page_token")
        if not token:
            return vulns


def _http_record(vuln_id: str) -> dict:
    import httpx

    response = httpx.get(OSV_VULN_URL.format(id=vuln_id), timeout=30)
    response.raise_for_status()
    return response.json()


def find_advisories(deps: list[Dependency], batch=None, record=None) -> list[tuple[Dependency, list[Advisory]]]:
    """For each vulnerable dependency, the advisories against the installed version. `batch` (query bodies ->
    the vulnerability IDs for each) and `record` (ID -> OSV record) are injectable."""
    batch, record = batch or _http_batch, record or _http_record
    found = batch([query_body(dep) for dep in deps])
    unique = sorted({vuln_id for ids in found for vuln_id in ids})
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = dict(zip(unique, pool.map(record, unique)))
    results: list[tuple[Dependency, list[Advisory]]] = []
    for dep, ids in zip(deps, found):
        name = osv_name(dep)
        advisories = dedupe([a for a in (parse_vuln(records[i], dep.ecosystem, name) for i in ids) if a.id])
        if advisories:
            results.append((dep, advisories))
    return results
