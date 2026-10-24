"""OpenPort eval adapter for jsliapark/preflight: plain-JSON inputs in, the facts its own tests check out.

Preflight's agents take a parsed ChangeSet and return pydantic results, and its standards pass reads
repo patterns from ChromaDB. These wrappers parse each diff with preflight's own parser, give the
standards pass a fixed in-memory pattern store, and reduce each result to what preflight's
test_evals.py asserts: how many comments and how many CRITICAL ones, the change's intent, and the PR
title's length and format.
"""

import re
import sys
import types

try:  # preflight's standards agent imports chromadb for retrieval only; a fixed pattern store replaces it
    import chromadb  # noqa: F401
except ImportError:
    sys.modules["chromadb"] = types.ModuleType("chromadb")
    sys.modules["chromadb"].Collection = object

from agents.diff_analyzer import analyze_diff  # noqa: E402
from agents.pr_description import generate_pr_description  # noqa: E402
from agents.review_agent import run_correctness_pass, run_security_pass  # noqa: E402
from agents.standards_agent import run_standards_pass  # noqa: E402
from core.diff_parser import parse_diff  # noqa: E402

PASSES = {"security": run_security_pass, "correctness": run_correctness_pass}
CONVENTIONAL = re.compile(r"^(feat|fix|docs|refactor|test|chore)(\([\w-]+\))?: \S")


class PatternStore:
    """Stands in for preflight's ChromaDB collection: every query returns the repo's patterns."""

    def __init__(self, patterns: list[str]):
        self.patterns = list(patterns)

    def query(self, query_texts, n_results):
        return {"documents": [self.patterns[:n_results]]}


def _summary(result) -> dict:
    severities = [comment.severity.value for comment in result.comments]
    return {"comments": len(severities), "critical": severities.count("critical"), "summary_length": len(result.summary or "")}


def review(review_pass: str, diff: str) -> dict:
    return _summary(PASSES[review_pass](parse_diff(diff)))


def standards(diff: str, patterns: list[str]) -> dict:
    return _summary(run_standards_pass(parse_diff(diff), PatternStore(patterns)))


def diff_intent(diff: str) -> dict:
    return {"intent": analyze_diff(parse_diff(diff)).intent.value}


def pr_title(diff: str) -> dict:
    title = generate_pr_description(parse_diff(diff)).title.strip()
    return {"title": title, "title_length": len(title), "conventional": bool(CONVENTIONAL.match(title))}
