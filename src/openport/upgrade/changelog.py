"""What changed between two versions of a package, quoted from its own changelog.

Tavily finds the changelog and reads it: the one the registry names, else a search of the package's own
pages, else a map of its docs site; Extract returns the page as markdown, JavaScript-rendered pages
included. OpenPort then cuts out the releases between the installed version and the new one and pulls
out the lines a reviewer has to see (removals, renames, changed behaviour), word for word. The PR shows
that digest for every upgraded package, and when the upgrade breaks the tests, Super reads those
releases in full before it fixes the code.
"""

import re
from dataclasses import dataclass, field

from packaging.version import InvalidVersion, Version

# A heading that starts with a version, after an optional "v", "Version", "Release" or the package's name.
_HEADING_VERSION = re.compile(r"^(?:version|release|v(?=\d))?\s*v?(\d+(?:\.\d+){1,3}(?:[-.]?(?:a|b|rc|alpha|beta|pre|dev|post)[-.]?\d*)?)\b", re.I)
_SETEXT = re.compile(r"^\s*[=\-~^\"'`*+#]{3,}\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
# What a reviewer must not miss, strongest first. Under a "Breaking changes" or "Removed" heading, anything counts.
STRONG = re.compile(r"\b(?:breaking|backwards?[- ]incompatible|incompatible|removed?|removals?|drop(?:s|ped)?|no longer|"
                    r"renamed?|now (?:returns?|raises?|requires?|defaults?)|must)\b", re.I)
WEAK = re.compile(r"\b(?:deprecat\w*|required|replaced|instead of|default\w* (?:is|was|to)|changed? (?:the )?default)\b", re.I)
NOTABLE_HEADINGS = re.compile(r"breaking|removed|removals|incompatible|migration|upgrade notes", re.I)
# A heading that only labels a section ("Removals and breaking changes", "Bug fixes") isn't a change to quote.
SECTION_LABEL = re.compile(r"^(?:deprecations?|removals?|breaking|backwards?[- ]incompatible|incompatible|features?|improvements?|"
                           r"bug ?fix(?:es)?|fix(?:es|ed)|changes?|changed|added|security|documentation|misc\w*|other|internal|"
                           r"migration|upgrade notes)\b", re.I)
FIX = re.compile(r"^(?:fix(?:es|ed)?|bug ?fix(?:es)?)\b", re.I)  # "Fixed bug breaking X" is a fix, not a break
MAX_NOTABLE = 8  # per package, in the PR
MAX_LINE = 300
MAX_TEXT = 6000  # releases in full, for Super


@dataclass
class Release:
    version: str
    notes: list[tuple[int, str]] = field(default_factory=list)  # (weight, line), the lines word for word from the changelog

    @property
    def notable(self) -> list[str]:
        """The lines, most important first."""
        return [text for _, text in sorted(self.notes, key=lambda note: -note[0])]


@dataclass
class Digest:
    name: str
    old: str
    new: str
    url: str
    title: str
    releases: list[Release]  # every release after `old` up to `new`, oldest first
    text: str  # those releases' full text, for Super

    def to_dict(self) -> dict:
        return {"name": self.name, "from": self.old, "to": self.new, "url": self.url, "title": self.title,
                "releases": [{"version": r.version, "notable": r.notable} for r in self.releases],
                "quotes": [{"version": version, "text": text} for version, text in self.quotes]}

    @property
    def quotes(self) -> list[tuple[str, str]]:
        """(version, line) for the PR: the MAX_NOTABLE most important lines, oldest release first."""
        ranked = [(-weight, n, i, release.version, text) for n, release in enumerate(self.releases)
                  for i, (weight, text) in enumerate(release.notes)]
        return [(version, text) for _, n, i, version, text in sorted(sorted(ranked)[:MAX_NOTABLE], key=lambda r: (r[1], r[0], r[2]))]


def _version(text: str, name: str = "") -> str | None:
    """The version a heading names, if it starts with one: "v2.0.0", "[9.0.0](link) (2022-12-21)", "PyJWT 2.4.0"."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [9.0.0](https://...) -> 9.0.0
    text = text.strip().strip("#*_`[] ").strip()
    if name and text.lower().startswith(name.lower()):
        text = text[len(name):].strip(" :-")
    match = _HEADING_VERSION.match(text)
    return match[1] if match else None


def _bold_heading(text: str) -> str | None:
    """A line that's only bold text (`**Documentation**`, escaped or not) works as a heading."""
    plain = re.sub(r"\\([*_])", r"\1", text.strip())
    match = re.fullmatch(r"(\*\*|__)([^*_]+)\1:?", plain)
    return match[2].strip() if match else None


def _heading(lines: list[str], i: int) -> str | None:
    """The heading text if line `i` is a heading: `## x`, an underlined line, or a line in bold."""
    stripped = lines[i].strip()
    if re.match(r"^#{1,6}\s+\S", stripped):
        return stripped.lstrip("#").strip()
    if stripped and i + 1 < len(lines) and _SETEXT.match(lines[i + 1]) and not _SETEXT.match(stripped) and not _BULLET.match(lines[i]):
        return stripped
    return _bold_heading(stripped)


def _headings(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, heading text) for each heading."""
    return [(i, text) for i in range(len(lines)) if (text := _heading(lines, i))]


def sections(markdown: str, name: str = "") -> list[tuple[str, list[str]]]:
    """Each release's (version, lines), in the page's order."""
    lines = markdown.splitlines()
    starts = [(i, version) for i, text in _headings(lines) if (version := _version(text, name))]
    return [(version, lines[i + 1 : (starts[n + 1][0] if n + 1 < len(starts) else len(lines))]) for n, (i, version) in enumerate(starts)]


def _parse(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


def _clean(text: str) -> str:
    """The words as written, without the markup: escapes, bold, link targets, commit hashes."""
    text = _BULLET.sub("", text).strip()
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", text)  # Markdown escapes
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # a link's text, not its URL
    text = re.sub(r"\[([^\]]+)\]\s*https?://[^\s)]+", r"\1", text)  # a link that lost its parentheses
    text = re.sub(r"\s*\(\s*\[?\b[0-9a-f]{7,40}\b\]?\s*\)", "", text)  # (commit hash)
    text = re.sub(r"`([^`]+)`_+", r"\1", text).replace("``", "`").replace("**", "").replace("¶", "")  # ¶: Sphinx's anchors
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1].rstrip() + "…"


def _items(lines: list[str]) -> list[tuple[str, bool, bool]]:
    """The section as (text, is_heading, under_a_notable_heading) items: a bullet or paragraph with its
    wrapped lines joined, or a heading."""
    items: list[list] = []
    under, previous_blank = False, True
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            previous_blank = True
            continue
        if _SETEXT.match(stripped):
            continue
        if text := _heading(lines, i):
            under = bool(NOTABLE_HEADINGS.search(text))
            items.append([text, True, False])
        elif _BULLET.match(line) or previous_blank or not items or items[-1][1]:
            items.append([stripped, False, under])
        else:
            items[-1][0] += " " + stripped  # a wrapped line belongs to the item above
        previous_blank = False
    return [(text, heading, under_notable) for text, heading, under_notable in items]


def notable(lines: list[str]) -> list[tuple[int, str]]:
    """(weight, line) for what a reviewer must see, in the section's order: what was removed, renamed or
    behaves differently, and everything under a "Breaking changes" (or "Removed", "Migration") heading."""
    picked: list[tuple[int, str]] = []
    for text, heading, under in _items(lines):
        cleaned = _clean(text)
        if len(cleaned.split()) < 3 or any(cleaned == seen for _, seen in picked) or (heading and SECTION_LABEL.match(cleaned)):
            continue
        weight = 2 * bool(STRONG.search(cleaned)) or bool(WEAK.search(cleaned))
        if FIX.match(cleaned) and not under:
            weight = 0
        weight += 2 * under
        if weight:
            picked.append((weight + heading, cleaned))
    return picked


def between(markdown: str, name: str, old: str, new: str) -> list[tuple[Version, str, list[str]]]:
    """The releases after `old` up to and including `new`, oldest first. Pre-releases count only if `new` is one."""
    low, high = _parse(old), _parse(new)
    if low is None or high is None:
        return []
    picked = {}
    for version, lines in sections(markdown, name):
        parsed = _parse(version)
        if parsed is None or not low < parsed <= high or (parsed.is_prerelease and not high.is_prerelease):
            continue
        picked.setdefault(parsed, (version, lines))  # a release listed twice (an index and its notes): the first
    return [(parsed, version, lines) for parsed, (version, lines) in sorted(picked.items())]


CHANGELOGISH = re.compile(r"change[-_ ]?log|changes|history|releases?|news|what.?s.?new", re.I)
GITHUB = re.compile(r"(?:git\+)?(?:https?://|ssh://git@|git@)?(?:www\.)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$", re.I)
FILES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGES.rst", "CHANGELOG.rst", "HISTORY.rst", "NEWS.md")
REGISTRIES = ("pypi.org", "npmjs.com", "www.npmjs.com", "registry.npmjs.org")


def _repos(homes: list[str]) -> list[tuple[str, str]]:
    return list(dict.fromkeys((m[1], m[2]) for home in homes if (m := GITHUB.match(home.strip()))))


def _display(url: str) -> str:
    """Where a reader should go: a repo file's GitHub page rather than its raw text."""
    raw = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/HEAD/(.+)$", url)
    return f"https://github.com/{raw[1]}/{raw[2]}/blob/HEAD/{raw[3]}" if raw else url


# A first heading that doesn't say whose changelog it is: GitHub's page chrome, or the file's own bare title
GENERIC_TITLE = re.compile(r"^(navigation menu|change ?log|changes|history|news|releases?|release notes)$", re.I)


def _title(markdown: str, name: str, url: str = "") -> str:
    first = next((_clean(line.lstrip("#")) for line in markdown.splitlines() if re.match(r"#{1,2}\s+\S", line.strip())), "")
    if first and not _version(first, name) and len(first) <= 80 and not GENERIC_TITLE.match(first):
        return first
    return f"{name} releases" if re.search(r"/releases/?$", url) else f"{name} changelog"


def first_candidates(homes: list[str]) -> list[str]:
    """Pages to read before searching: the changelog the registry names, then the repo's own changelog files."""
    declared = [home for home in homes if CHANGELOGISH.search(home) and not any(r in home for r in REGISTRIES)]
    files = [f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}" for owner, repo in _repos(homes) for name in FILES]
    releases = [f"https://github.com/{owner}/{repo}/releases" for owner, repo in _repos(homes)]
    return list(dict.fromkeys([*declared, *files, *releases]))


def searched_candidates(name: str, homes: list[str], search) -> list[str]:
    """Changelog-looking pages among the package's own pages, by a Tavily search."""
    from .research import official

    domains = list(dict.fromkeys(m[1].lower() for home in homes if (m := re.match(r"(?:[a-z+]+://)?(?:www\.)?([^/:]+)", home.strip()))))
    found = []
    for result in search(f"{name} changelog release notes", domains or ["github.com"]):
        url, title = result.get("url") or "", result.get("title") or ""
        if url and official(url, name, homes) and CHANGELOGISH.search(f"{url} {title}"):
            found.append(url)
    return found


def mapped_candidates(homes: list[str], site_map) -> list[str]:
    """Changelog pages on the package's docs site (not its repo or registry page), by a Tavily map."""
    docs = [home for home in homes if not GITHUB.match(home.strip()) and not any(r in home for r in REGISTRIES)]
    found = []
    for site in docs[:1]:
        found += site_map(site, [r".*(change|release|history|news).*"])
    return found


def changelog_digest(upgrade, *, search=None, extract=None, site_map=None, homes_for=None, max_reads: int = 10) -> Digest | None:
    """The digest for one upgrade, from the first page that lists its releases: the declared changelog or the
    repo's changelog file, else a search of the package's pages, else a map of its docs site."""
    from ..pricing import tavily_extract, tavily_map, tavily_search
    from .registry import fetch_homes

    search, extract, site_map = search or tavily_search, extract or tavily_extract, site_map or tavily_map
    name, old, new = upgrade.dep.name, upgrade.dep.version, upgrade.target.version
    try:
        homes = (homes_for or fetch_homes)(upgrade.dep)
    except Exception:  # the registry is down: nothing to start from
        return None
    tried: set[str] = set()
    for stage in (lambda: first_candidates(homes), lambda: searched_candidates(name, homes, search), lambda: mapped_candidates(homes, site_map)):
        batch = [url for url in stage() if url not in tried][:max_reads]
        tried.update(batch)
        if not batch:
            continue
        pages = extract(batch)
        for url in batch:
            markdown = pages.get(url)
            if markdown and (found := digest(name, old, new, _display(url), _title(markdown, name, url), markdown)):
                return found
    return None


def digest(name: str, old: str, new: str, url: str, title: str, markdown: str) -> Digest | None:
    """The releases between `old` and `new` in a changelog page, or None if the page doesn't list them."""
    releases = between(markdown, name, old, new)
    if not releases:
        return None
    # Oldest first: breaking changes cluster in the first major release after `old`, so they survive the cut.
    text = "\n\n".join(f"## {version}\n" + "\n".join(lines).strip() for _, version, lines in releases)
    return Digest(name, old, new, url, title, [Release(version, notable(lines)) for _, version, lines in releases], text[:MAX_TEXT])
