"""The fixer: Nemotron Super repairs code that a security upgrade broke, as exact search-and-replace edits."""

import json
import re
import time
from pathlib import Path

from openai import OpenAI

from .. import tokenfactory
from ..citations import parse_citations, verified
from ..runtime.openport_llm import UsageMeter
from ..scan.findings import is_test_path
from .apply import SKIP_DIRS, Edit

SYSTEM = """You fix application code that broke when dependencies were upgraded to fix security advisories.

You get the upgrades (package, old and new version), excerpts from their changelogs or advisories, the failing test output, and the repo's source files.

Rules:
- Change application code only, never tests: the tests define correct behaviour.
- Make the smallest change that works with the new version. Never pin the old version back or undo the security fix.
- Each edit replaces one exact snippet: copy `search` from the file character for character, including indentation, and make it long enough to appear only once.
- If the new version needs a file the repo doesn't have yet (a framework that now requires one, say), give an edit with an empty `search` and the whole new file as `replace`.
- If excerpts from changelogs are included, use them to understand what changed. Cite an excerpt in sources only if your fix follows something it says, with that sentence copied word for word as the quote. Citations are checked against the excerpts, and an empty list is normal.

Respond with JSON only:
{"edits": [{"file": "path/in/repo.py", "search": "exact text", "replace": "new text"}], "rationale": "one sentence on what broke and why the fix is right", "sources": [{"url": "...", "quote": "the sentence you followed"}]}"""

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs")
SOURCE_BUDGET = 40_000  # characters of source sent per request
# Super reasons before it answers, and its reasoning counts against max_tokens: with a big repo's sources it can
# spend the whole budget thinking and answer nothing. It gets room for both, and if it still runs out, the same
# question again with reasoning off.
MAX_TOKENS = 16_384


class FixerSilent(RuntimeError):
    """Super ran out of tokens before giving any answer, with reasoning on and off."""


def source_files(folder: Path, test_output: str) -> dict[str, str]:
    """The repo's source files (not tests), the ones named in the failure first, within a size budget."""
    files = [p for p in sorted(folder.rglob("*"))
             if p.is_file() and p.suffix in SOURCE_SUFFIXES and not SKIP_DIRS & set(p.relative_to(folder).parts)]
    relative = {p: p.relative_to(folder).as_posix() for p in files}
    candidates = [p for p in files if not is_test_path(relative[p])]
    candidates.sort(key=lambda p: relative[p] not in test_output)  # named in the traceback first
    chosen, used = {}, 0
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > SOURCE_BUDGET:
            continue
        chosen[relative[path]] = text
        used += len(text)
    return chosen


def parse_fix(text: str) -> tuple[list[Edit], str, list[dict]]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    edits = [  # an empty search with a replacement is a new file
        Edit(str(e["file"]), str(e.get("search") or ""), str(e.get("replace", "")))
        for e in data.get("edits", []) if isinstance(e, dict) and e.get("file") and (e.get("search") or e.get("replace"))
    ]
    return edits, str(data.get("rationale") or "").strip(), parse_citations(data.get("sources"))


def make_fixer(client: OpenAI | None = None, model: str | None = None, meter: UsageMeter | None = None):
    client = client or tokenfactory.client()
    model = model or tokenfactory.tiers()["super"]

    def fix(upgrades: list[dict], research: list[dict], test_output: str, files: dict[str, str], notes: str = "") -> tuple[list[Edit], str, list[dict]]:
        prompt = "Upgrades:\n" + "\n".join(f"- {u['name']} {u['from']} -> {u['to']}" for u in upgrades)
        if research:
            prompt += "\n\nWhat changed (found with Tavily):\n" + "\n".join(f"- {s['title']} ({s['url']}): {s['excerpt']}" for s in research)
        prompt += f"\n\nFailing tests:\n<<<\n{test_output[-6000:]}\n>>>"
        if notes:
            prompt += f"\n\nYour previous attempt:\n{notes}"
        prompt += "\n\nSource files:\n" + "\n\n".join(f"=== {name} ===\n{text}" for name, text in files.items())
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        for extra in ({}, {"extra_body": tokenfactory.reasoning(False)}):
            start = time.perf_counter()
            response = client.chat.completions.create(model=model, messages=messages, max_tokens=MAX_TOKENS, **extra)
            if meter is not None:
                meter.record(model, response.usage, time.perf_counter() - start)
            choice = response.choices[0]
            edits, rationale, cited = parse_fix(choice.message.content or "")
            if edits or rationale or choice.finish_reason != "length":
                return edits, rationale, verified(cited, research)  # only quotes that are really on the pages
        raise FixerSilent("Nemotron Super ran out of tokens before answering, with reasoning on and then off")

    return fix
