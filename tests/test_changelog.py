"""Tests for the changelog digest: finding a package's changelog with Tavily, and quoting what changed between two versions."""

from openport.upgrade import Upgrade
from openport.upgrade.changelog import changelog_digest, digest, first_candidates
from openport.upgrade.lockfiles import Dependency
from openport.upgrade.select import Target

# PyJWT's CHANGELOG.rst, as Tavily returns it: underlined headings, sub-headings, paragraphs wrapped at 72 columns.
RST = """Changelog
=========

`v2.1.0 <https://github.com/jpadilla/pyjwt/compare/2.0.1...2.1.0>`__
--------------------------------------------------------------------

Changed
~~~~~~~

- Allow claims validation without making JWT signature validation mandatory.

`v2.0.0 <https://github.com/jpadilla/pyjwt/compare/1.7.1...2.0.0>`__
--------------------------------------------------------------------

Changed
~~~~~~~

Drop support for Python 2 and Python 3.0-3.5
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Python 3.5 is EOL so we decide to drop its support. Version ``1.7.1`` is
the last one supporting Python 3.0-3.5.

- Tokens are returned as string instead of a byte string
- Fixed bug breaking the ability to decode tokens with a leeway

`v1.7.1 <https://github.com/jpadilla/pyjwt/compare/1.7.0...1.7.1>`__
--------------------------------------------------------------------

- Removed something long ago
"""

# requests' HISTORY.md: Tavily escapes the bold sub-headings, and they sit right under the bullets above them.
MARKDOWN = """Release History
===============

2.32.0 (2024-05-20)
-------------------

\\*\\*Security\\*\\*
- Fixed an issue where setting `verify=False` on the first request from a
  Session will cause subsequent requests to also ignore cert verification.

\\*\\*Deprecations\\*\\*
- Requests has officially dropped support for CPython 3.7 (#6642)
\\*\\*Documentation\\*\\*
- Numerous documentation improvements
"""

# jsonwebtoken's CHANGELOG.md, with a commit link that lost its parentheses on the way.
GITHUB = """# Change Log

## 9.0.0 - 2022-12-21

**Breaking changes: See [Migration from v8 to v9](https://github.com/auth0/node-jsonwebtoken/wiki/Migration-Notes:-v8-to-v9)**

### Breaking changes

- Removed support for Node versions 11 and below.
- The verify() function no longer accepts unsigned tokens by default. ([834503079514b72264fd13023a3b8d648afd6a16]https://github.com/auth0/node-jsonwebtoken/commit/834503079514b72264fd13023a3b8d648afd6a16)
- Key types must be valid for the signing / verification algorithm

### Security fixes

- security: fixes `Arbitrary File Write via verify function` - CVE-2022-23529

## 8.5.1 - 2019-03-18

- Something from long ago
"""


def test_only_the_releases_in_between_and_what_a_reviewer_must_see():
    found = digest("PyJWT", "1.7.1", "2.1.0", "https://github.com/jpadilla/pyjwt/blob/HEAD/CHANGELOG.rst", "PyJWT changelog", RST)
    assert [r.version for r in found.releases] == ["2.0.0", "2.1.0"]  # oldest first; 1.7.1 is what's installed
    assert found.releases[0].notable == [
        "Drop support for Python 2 and Python 3.0-3.5",  # a sub-heading counts, and is weighed highest
        "Python 3.5 is EOL so we decide to drop its support. Version `1.7.1` is the last one supporting Python 3.0-3.5.",
        "Tokens are returned as string instead of a byte string",
    ]  # the wrapped paragraph comes back whole; "Fixed bug breaking ..." is a fix, not a break
    assert found.releases[1].notable == []
    assert found.text.startswith("## 2.0.0") and "## 2.1.0" in found.text  # in full for Super, oldest first
    assert digest("PyJWT", "2.1.0", "2.2.0", "u", "t", RST) is None  # the page doesn't reach that far


def test_bold_sub_headings_and_link_leftovers_dont_end_up_in_the_quotes():
    found = digest("requests", "2.31.0", "2.32.0", "u", "t", MARKDOWN)
    assert found.quotes == [("2.32.0", "Requests has officially dropped support for CPython 3.7 (#6642)")]
    jwt = digest("jsonwebtoken", "8.5.1", "9.0.0", "u", "t", GITHUB)
    assert jwt.quotes == [  # under "Breaking changes": every line counts; the headings that only label it don't
        ("9.0.0", "Removed support for Node versions 11 and below."),
        ("9.0.0", "The verify() function no longer accepts unsigned tokens by default."),
        ("9.0.0", "Key types must be valid for the signing / verification algorithm"),
    ]


def upgrade(name, old, new, ecosystem):
    return Upgrade(Dependency(name, old, ecosystem, "lock"), [], Target(new, None, 0), "upgrade")


def test_the_cheapest_tavily_call_that_finds_the_changelog_wins():
    calls = []

    def extract(urls):
        calls.append(("extract", len(urls)))
        pages = {"https://raw.githubusercontent.com/auth0/node-jsonwebtoken/HEAD/CHANGELOG.md": GITHUB,
                 "https://pyjwt.readthedocs.io/en/stable/changelog.html": RST, "https://docs.acme.dev/history/": RST}
        return {url: pages[url] for url in urls if url in pages}

    def search(query, domains):
        calls.append(("search", tuple(domains)))
        if "PyJWT" in query:
            return [{"url": "https://pyjwt.readthedocs.io/en/stable/changelog.html", "title": "Changelog — PyJWT"},
                    {"url": "https://example.com/pyjwt-changelog", "title": "Someone else's notes"}]
        return []

    def site_map(url, paths):
        calls.append(("map", url))
        return ["https://docs.acme.dev/history/"]

    homes = {"jsonwebtoken": ["https://github.com/auth0/node-jsonwebtoken", "https://www.npmjs.com/package/jsonwebtoken"],
             "PyJWT": ["https://pyjwt.readthedocs.io"], "acmejwt": ["https://docs.acme.dev"]}
    tools = {"search": search, "extract": extract, "site_map": site_map, "homes_for": lambda dep: homes[dep.name]}

    found = changelog_digest(upgrade("jsonwebtoken", "8.5.1", "9.0.0", "npm"), **tools)
    assert found.url == "https://github.com/auth0/node-jsonwebtoken/blob/HEAD/CHANGELOG.md" and found.title == "Change Log"
    assert calls == [("extract", 8)]  # the repo's changelog files, read at once: no search needed
    calls.clear()
    found = changelog_digest(upgrade("PyJWT", "1.7.1", "2.0.0", "PyPI"), **tools)
    assert found.url == "https://pyjwt.readthedocs.io/en/stable/changelog.html"  # found by a search of its own pages
    assert calls == [("search", ("pyjwt.readthedocs.io",)), ("extract", 1)]  # someone else's page isn't read
    calls.clear()
    assert changelog_digest(upgrade("acmejwt", "1.7.1", "2.0.0", "PyPI"), **tools).url == "https://docs.acme.dev/history/"
    assert [call[0] for call in calls] == ["search", "map", "extract"]  # a map of its docs site, last


def test_a_declared_changelog_is_read_before_guessing_at_the_repo():
    homes = ["https://requests.readthedocs.io", "https://github.com/psf/requests", "https://github.com/psf/requests/blob/main/HISTORY.md"]
    assert first_candidates(homes)[:2] == ["https://github.com/psf/requests/blob/main/HISTORY.md",
                                           "https://raw.githubusercontent.com/psf/requests/HEAD/CHANGELOG.md"]
