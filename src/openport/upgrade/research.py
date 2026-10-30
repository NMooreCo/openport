"""Research an upgrade with Tavily: what changed between the two versions, and the advisory itself.

Changelogs and migration notes are newer than any model's training data, so Super gets the relevant
excerpts, with sources, before it fixes code the upgrade broke, and the PR cites them. Only the
package's own pages (its repo, docs and registry page) and advisory databases count: a search for
a popular package also finds other projects' bump PRs, which say nothing about what changed.
"""

import re

from ..pricing import tavily_search
from . import Upgrade
from .registry import fetch_homes

UPGRADE_DOMAINS = ["github.com", "pypi.org", "readthedocs.io", "osv.dev", "npmjs.com", "nvd.nist.gov"]
ADVISORY_PAGES = ["osv.dev/vulnerability/", "nvd.nist.gov/vuln/detail/", "github.com/advisories/"]
HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "pypi.org", "npmjs.com", "readthedocs.io"}  # too broad to be a home
# Pages anyone can write, even in the package's own repo: an issue, a pull request or a discussion says what someone
# claims, not what changed, and a planted one could steer the fix.
OPEN_PAGES = re.compile(r"/(?:issues|pulls?|discussions|merge_requests|pull-requests)(?:/|$)")
EXCERPT_CHARS = 900
MAX_SOURCES = 5


def _bare(url: str) -> str:
    """A URL without scheme, "www." or a trailing slash or ".git", lowercased, for prefix matching."""
    url = re.sub(r"^[a-z+]+://", "", url.strip().lower()).removeprefix("www.")
    return url.removesuffix("/").removesuffix(".git").rstrip("/")


def official(url: str, name: str, homes: list[str]) -> bool:
    """Whether `url` is the package's own page (its repo, docs or registry page) or an advisory database's."""
    bare, slug = _bare(url), re.sub(r"[-_.]+", "-", name.lower())
    if OPEN_PAGES.search(bare) and not any(bare.startswith(p) for p in ADVISORY_PAGES):
        return False
    prefixes = [h for h in map(_bare, homes) if h not in HOSTS]
    prefixes += [f"{slug}.readthedocs.io", f"pypi.org/project/{slug}", f"npmjs.com/package/{name.lower()}"]
    return any(bare == p or bare.startswith(p + "/") for p in prefixes) or any(bare.startswith(p) for p in ADVISORY_PAGES)


def research_upgrade(upgrade: Upgrade, search=None, homes_for=None) -> list[dict]:
    """Up to five relevant pages (changelog, migration notes, advisory) as {"title", "url", "excerpt"}."""
    search = search or tavily_search
    try:
        homes = (homes_for or fetch_homes)(upgrade.dep)
    except Exception:  # the registry is down: the package's docs and advisory pages still count
        homes = []
    name, old, new = upgrade.dep.name, upgrade.dep.version, upgrade.target.version
    queries = [f"{name} {new} changelog breaking changes migrating from {old}"]
    if upgrade.advisory_ids:
        queries.append(f"{name} {upgrade.advisory_ids[0]} security advisory")
    sources, seen = [], set()
    for query in queries:
        for result in search(query, UPGRADE_DOMAINS):
            url, content = result.get("url"), " ".join((result.get("content") or "").split())
            title = result.get("title") or url
            if not url or not content or url in seen:
                continue
            if name.lower() not in f"{title} {url} {content}".lower() or not official(url, name, homes):
                continue  # a page about something else, or someone else's page about it
            seen.add(url)
            sources.append({"title": title, "url": url, "excerpt": content[:EXCERPT_CHARS]})
    return sources[:MAX_SOURCES]
