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
- If the new version needs a file the repo doesn't have yet (a framework that now requires one, say), give an edit with an empty `search` and the whole new file as `replace`. Write the least it needs, and don't repeat what existing files already do: read them first. (A required root layout, when a nested layout already renders the html and body tags, only passes its children through.)
- A previous attempt, if one is described, was undone: the files are as shown here, not as that attempt left them.
- If excerpts from changelogs are included, use them to understand what changed. Cite an excerpt in sources only if your fix follows something it says, with that sentence copied word for word as the quote. Citations are checked against the excerpts, and an empty list is normal.

Respond with JSON only:
{"edits": [{"file": "path/in/repo.py", "search": "exact text", "replace": "new text"}], "rationale": "one sentence on what broke and why the fix is right", "sources": [{"url": "...", "quote": "the sentence you followed"}]}"""

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs")
SOURCE_BUDGET = 40_000  # characters of source sent per request
# Super reasons before it answers, and its reasoning counts against max_tokens: with a big repo's sources it can
# spend the whole budget thinking and answer nothing. It gets room for both, and if it still runs out, the same
# question again with reasoning off.
MAX_TOKENS = 16_384
# Low, for code edits: replayed on learn-claude-code's Next.js 16.4 break, Ultra wrote the one-line root layout
# 3 times in 3 at 0.2, and once in 3 at its default.
TEMPERATURE = 0.2


class FixerSilent(RuntimeError):
    """Super ran out of tokens before giving any answer, with reasoning on and off."""


def project_paths(folder: Path, limit: int = 800) -> list[str]:
    """Every file in the project, as paths, so a fix names real ones (a stylesheet's, say) instead of guessing."""
    paths = [p.relative_to(folder).as_posix() for p in sorted(folder.rglob("*"))
             if p.is_file() and not (SKIP_DIRS | {".git"}) & set(p.relative_to(folder).parts)]
    return paths[:limit]


def source_files(folder: Path, test_output: str) -> dict[str, str]:
    """The repo's source files (not tests) within a size budget: the ones the failure names first, then the ones
    beside them (a framework's error in src/app/page.tsx is usually about the files in src/app), then the rest."""
    files = [p for p in sorted(folder.rglob("*"))
             if p.is_file() and p.suffix in SOURCE_SUFFIXES and not SKIP_DIRS & set(p.relative_to(folder).parts)]
    relative = {p: p.relative_to(folder).as_posix() for p in files}
    candidates = [p for p in files if not is_test_path(relative[p])]
    named = [p for p in candidates if relative[p] in test_output]
    near = {p.parent for p in named} - {folder}  # a config file at the root (next.config.ts) makes nothing near
    candidates.sort(key=lambda p: 0 if p in named else 1 if any(d == p.parent or d in p.parents for d in near) else 2)
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


def make_fixer(client: OpenAI | None = None, model: str | None = None, meter: UsageMeter | None = None, stronger: str | None = None):
    """Super's fix first; if it doesn't make the tests pass, the next rounds go to `stronger` (Ultra), the ladder
    the port climbs. On learn-claude-code, Super rewrote both of Next.js's layouts where Ultra added the one line of
    layout 16.4 asks for (seen live). The model each round used is kept on the fixer as `last_model`."""
    client = client or tokenfactory.client()
    if model is None:
        tiers = tokenfactory.tiers()
        model, stronger = tiers["super"], stronger or tiers.get("ultra")
    stronger = stronger or model

    def fix(upgrades: list[dict], research: list[dict], test_output: str, files: dict[str, str], notes: str = "",
            paths: list[str] = (), attempt: int = 0) -> tuple[list[Edit], str, list[dict]]:
        chosen = model if attempt == 0 else stronger
        fix.last_model = chosen
        prompt = "Upgrades:\n" + "\n".join(f"- {u['name']} {u['from']} -> {u['to']}" for u in upgrades)
        if research:
            prompt += "\n\nWhat changed (found with Tavily):\n" + "\n".join(f"- {s['title']} ({s['url']}): {s['excerpt']}" for s in research)
        prompt += f"\n\nFailing tests:\n<<<\n{test_output[-6000:]}\n>>>"
        if notes:
            prompt += f"\n\nYour previous attempt:\n{notes}"
        if paths:
            prompt += "\n\nEvery file in the project (paths only):\n" + "\n".join(paths)
        prompt += "\n\nSource files:\n" + "\n\n".join(f"=== {name} ===\n{text}" for name, text in files.items())
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        for extra in ({}, {"extra_body": tokenfactory.reasoning(False)}):
            start = time.perf_counter()
            response = client.chat.completions.create(model=chosen, messages=messages, max_tokens=MAX_TOKENS, temperature=TEMPERATURE, **extra)
            if meter is not None:
                meter.record(chosen, response.usage, time.perf_counter() - start)
            choice = response.choices[0]
            edits, rationale, cited = parse_fix(choice.message.content or "")
            if edits or rationale or choice.finish_reason != "length":
                return edits, rationale, verified(cited, research)  # only quotes that are really on the pages
        raise FixerSilent(f"{chosen} ran out of tokens before answering, with reasoning on and then off")

    fix.last_model = model
    return fix
