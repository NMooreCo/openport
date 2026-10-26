"""Research: look up NVIDIA's own guidance for a model with Tavily, so the engineer's fix can follow it.

Before the engineer proposes a fix for a call on a Nemotron model, Tavily searches NVIDIA's docs, the
models' cards and Token Factory's docs for guidance on the kind of call it is (JSON output, tool
calling, free text), or on the failure itself when reasoning text leaks into the answer. The engineer
gets the excerpts with their sources and cites the ones it used, so the PR can show where a fix came from.
"""

import re
from dataclasses import dataclass, field
from typing import Callable

from ..evals.runner import CaseResult
from ..evals.spec import CallSite

NVIDIA_DOMAINS = ["docs.nvidia.com", "developer.nvidia.com", "build.nvidia.com", "huggingface.co", "docs.tokenfactory.nebius.com"]
QUERIES = {
    "json": "{model} JSON structured output response_format recommended settings",
    "tools": "{model} tool calling function calling recommended settings",
    "reasoning": "{model} reasoning mode enable_thinking chat template when to turn reasoning off",
    # Not "recommended temperature": asking for sampling defaults pulled every free-text fix toward them.
    "text": "{model} system prompt instruction following and output formatting best practices",
}
JSON_OUTPUTS = {"json_object", "json_schema", "json_in_text"}
EXCERPT_CHARS = 700
MAX_SOURCES = 3

# search(query, domains) -> [{"url", "title", "content"}]
Search = Callable[[str, list[str]], list[dict]]


@dataclass
class Guidance:
    model: str
    topic: str  # json | tools | reasoning | text
    query: str
    sources: list[dict] = field(default_factory=list)  # {"title", "url", "excerpt"}


# researcher(site, model, failures) -> guidance, or None when nothing relevant was found
Researcher = Callable[[CallSite, str, list[tuple[dict, CaseResult]]], Guidance | None]


_FAMILY = re.compile(r"nemotron[-_ ]?(\d+(?:[._]\d+)?)[-_ ](nano|super|ultra|lightning)([-_ ]vl\b)?", re.IGNORECASE)


def model_family(model: str) -> str:
    """"nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B" -> "NVIDIA Nemotron 3 Nano", the way NVIDIA's docs name it
    ("nvidia/Nemotron-3.5-Super-VL" -> "NVIDIA Nemotron 3.5 Super VL")."""
    match = _FAMILY.search(model)
    if not match:
        return model.split("/")[-1]
    return f"NVIDIA Nemotron {match.group(1).replace('_', '.')} {match.group(2).capitalize()}{' VL' if match.group(3) else ''}"


def about_model(model: str, text: str) -> bool:
    """Does a page talk about this exact model? Settings differ between Nano and Nano Omni, Super 3 and Super 3.5 VL."""
    match = _FAMILY.search(model)
    if not match:
        return True  # not a Nemotron name that can be checked
    version = re.escape(match.group(1).replace("_", ".")).replace(r"\.", r"[._]")
    size = match.group(2)
    # The vision model's pages must say VL; a text model's pages mustn't be about its Omni or VL sibling.
    sep, variant = r"[\s_-]*", (r"[\s_-]*(?:vl\b|vision)" if match.group(3) else r"(?![\s_-]*(?:omni|vl\b|vision))")
    patterns = (
        rf"nemotron{sep}{version}(?![._]?\d){sep}(?:\d+b{sep}(?:a\d+b{sep})?)?{size}{variant}",  # Nemotron-3-Nano(-30B-A3B)
        rf"nemotron{sep}{size}{sep}v?{version}(?![._]?\d){variant}",  # Nemotron Nano v3
    )
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def topic_for(output: str, failures: list[tuple[dict, CaseResult]]) -> str:
    """What to look up. Reasoning text leaking into the answer outranks the call's own shape."""
    for _, result in failures:
        seen = f"{result.output if isinstance(result.output, str) else ''} {result.error or ''}"
        if "<think>" in seen or "</think>" in seen:
            return "reasoning"
    if output == "tools":
        return "tools"
    return "json" if output in JSON_OUTPUTS else "text"


def make_researcher(search: Search | None = None) -> Researcher:
    """A researcher that searches once per model and topic, and reuses the answer for the rest of the run."""
    if search is None:
        from ..pricing import tavily_search

        search = tavily_search
    found: dict[tuple[str, str], Guidance] = {}

    def research(site: CallSite, model: str, failures: list[tuple[dict, CaseResult]]) -> Guidance | None:
        topic = topic_for(site.output, failures)
        if (model, topic) not in found:
            query = QUERIES[topic].format(model=model_family(model))
            results = search(query, NVIDIA_DOMAINS)
            sources = [
                {"title": r.get("title") or r["url"], "url": r["url"], "excerpt": " ".join((r.get("content") or "").split())[:EXCERPT_CHARS]}
                for r in results
                if r.get("url") and (r.get("content") or "").strip()
                and about_model(model, f"{r.get('title') or ''} {r['url']} {r.get('content') or ''}")
            ][:MAX_SOURCES]
            found[(model, topic)] = Guidance(model, topic, query, sources)
        guidance = found[(model, topic)]
        return guidance if guidance.sources else None

    return research
