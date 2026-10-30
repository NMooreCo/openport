"""Fetch each candidate version's release date from the package registry (PyPI or npm).

Dates feed the "at least N days old" safety check in selection. The JSON parsing is pure and tested;
the HTTP calls are thin wrappers.
"""

from datetime import datetime

from .lockfiles import Dependency
from .osv import osv_name

PYPI_URL = "https://pypi.org/pypi/{name}/json"
NPM_URL = "https://registry.npmjs.org/{name}"


def _parse_dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_pypi_releases(data: dict) -> dict[str, datetime]:
    """Each version mapped to the latest upload time of its files: a file added to an old release later (a stolen
    token can do that) is as new as its upload. A release whose files are all yanked is left out, as is one with a
    file whose time isn't given: neither can be shown to be old enough."""
    releases: dict[str, datetime] = {}
    for version, files in (data.get("releases") or {}).items():
        files = [f for f in files or [] if isinstance(f, dict)]
        if not files or all(f.get("yanked") for f in files):
            continue
        parsed = [_parse_dt(f.get("upload_time_iso_8601") or f.get("upload_time")) for f in files]
        if all(parsed):
            releases[version] = max(parsed)
    return releases


def parse_npm_releases(data: dict, deprecated: bool = False) -> dict[str, datetime]:
    """Each version's publish time. npm's `time` keeps versions since unpublished, which can't be installed: only the
    ones still in `versions` count, and a deprecated one only with `deprecated` (a lockfile may hold one; a fix
    shouldn't move to one)."""
    versions = data.get("versions") or {}
    releases: dict[str, datetime] = {}
    for version, stamp in (data.get("time") or {}).items():
        info = versions.get(version)
        if not isinstance(info, dict) or (info.get("deprecated") and not deprecated):
            continue
        dt = _parse_dt(stamp)
        if dt:
            releases[version] = dt
    return releases


def parse_pypi_homes(data: dict) -> list[str]:
    """The project's own URLs (repo, docs, homepage) as published on PyPI."""
    info = data.get("info") or {}
    urls = [*(info.get("project_urls") or {}).values(), info.get("home_page"), info.get("package_url")]
    return [url for url in urls if isinstance(url, str) and url.strip()]


def parse_npm_homes(data: dict) -> list[str]:
    repository = data.get("repository")
    repo = repository.get("url") if isinstance(repository, dict) else repository
    urls = [data.get("homepage"), repo.removeprefix("git+") if isinstance(repo, str) else None]
    return [url for url in urls if isinstance(url, str) and url.strip()]


def _registry(dep: Dependency) -> dict:
    import httpx

    url = PYPI_URL.format(name=osv_name(dep)) if dep.ecosystem == "PyPI" else NPM_URL.format(name=dep.name)
    response = httpx.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_releases(dep: Dependency) -> dict[str, datetime]:
    """The releases a fix can move to, with their dates."""
    data = _registry(dep)
    return parse_pypi_releases(data) if dep.ecosystem == "PyPI" else parse_npm_releases(data)


def fetch_release_dates(dep: Dependency) -> dict[str, datetime]:
    """The dates of every release a lockfile can hold, deprecated npm versions included: what a bump's resolution is
    checked against (bump.too_new)."""
    data = _registry(dep)
    return parse_pypi_releases(data) if dep.ecosystem == "PyPI" else parse_npm_releases(data, deprecated=True)


def fetch_homes(dep: Dependency) -> list[str]:
    data = _registry(dep)
    return parse_pypi_homes(data) if dep.ecosystem == "PyPI" else parse_npm_homes(data)
