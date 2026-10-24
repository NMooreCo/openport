"""Research an upgrade with Tavily: what changed between the two versions, and the advisory itself.

Changelogs and migration notes are newer than any model's training data, so Super gets the relevant
excerpts, with sources, before it fixes code the upgrade broke, and the PR cites them.
"""

from ..pricing import tavily_search
from . import Upgrade

UPGRADE_DOMAINS = ["github.com", "pypi.org", "readthedocs.io", "osv.dev", "npmjs.com", "nvd.nist.gov"]
EXCERPT_CHARS = 900
MAX_SOURCES = 5


def research_upgrade(upgrade: Upgrade, search=None) -> list[dict]:
    """Up to five relevant pages (changelog, migration notes, advisory) as {"title", "url", "excerpt"}."""
    search = search or tavily_search
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
            if name.lower() not in f"{title} {url} {content}".lower():
                continue  # a page about something else
            seen.add(url)
            sources.append({"title": title, "url": url, "excerpt": content[:EXCERPT_CHARS]})
    return sources[:MAX_SOURCES]
