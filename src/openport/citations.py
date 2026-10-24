"""Citations a model gives for a change, kept only when the page really says what the model quotes.

The tuning engineer and the upgrade fixer both get excerpts of pages Tavily found, and cite the
sentence they followed. A citation counts only if that sentence appears in the excerpt it cites
(ignoring case, spacing and quote styles), so a PR never links a page that doesn't say what the
change relied on.
"""

import re

MIN_QUOTE_WORDS = 4  # a real sentence, not a keyword that appears on any page
_FANCY = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-", " ": " ", "‑": "-"})


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_FANCY)).strip().lower()


def parse_citations(value) -> list[dict]:
    """[{"url", "quote"}] from a model's JSON. A bare URL has no quote, so it can't be verified."""
    citations = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            citations.append({"url": item["url"], "quote": str(item.get("quote") or "").strip()})
        elif isinstance(item, str):
            citations.append({"url": item, "quote": ""})
    return citations


def verified(citations: list[dict], pages: list[dict]) -> list[dict]:
    """The citations whose quote is in the excerpt of the page they cite, as {"title", "url", "quote"}."""
    by_url = {page["url"]: page for page in pages}
    kept = []
    for citation in citations:
        page = by_url.get(citation["url"])
        quote = _plain(citation.get("quote", "")).strip(" \"'.…")
        if page and len(quote.split()) >= MIN_QUOTE_WORDS and quote in _plain(page.get("excerpt", "")):
            kept.append({"title": page["title"], "url": citation["url"], "quote": citation["quote"].strip()})
    return kept
