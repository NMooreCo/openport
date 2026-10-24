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


def fetch_releases(dep: Dependency) -> dict[str, datetime]:
    import httpx

    if dep.ecosystem == "PyPI":
        response = httpx.get(PYPI_URL.format(name=osv_name(dep)), timeout=30)
        response.raise_for_status()
        return parse_pypi_releases(response.json())
    response = httpx.get(NPM_URL.format(name=dep.name), timeout=30)
    response.raise_for_status()
    return parse_npm_releases(response.json())
