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
    """Each version mapped to its earliest file upload time."""
    releases: dict[str, datetime] = {}
    for version, files in (data.get("releases") or {}).items():
        stamps = [f.get("upload_time_iso_8601") or f.get("upload_time") for f in files if isinstance(f, dict)]
        parsed = [dt for dt in (_parse_dt(s) for s in stamps if s) if dt]
        if parsed:
            releases[version] = min(parsed)
    return releases


def parse_npm_releases(data: dict) -> dict[str, datetime]:
    releases: dict[str, datetime] = {}
    for version, stamp in (data.get("time") or {}).items():
        if version in ("created", "modified"):
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
    data = _registry(dep)
    return parse_pypi_releases(data) if dep.ecosystem == "PyPI" else parse_npm_releases(data)


def fetch_homes(dep: Dependency) -> list[str]:
    data = _registry(dep)
    return parse_pypi_homes(data) if dep.ecosystem == "PyPI" else parse_npm_homes(data)
