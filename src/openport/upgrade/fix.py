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
from .apply import Edit

SYSTEM = """You fix application code that broke when dependencies were upgraded to fix security advisories.

You get the upgrades (package, old and new version), excerpts from their changelogs or advisories, the failing test output, and the repo's source files.

Rules:
- Change application code only, never tests: the tests define correct behaviour.
- Make the smallest change that works with the new version. Never pin the old version back or undo the security fix.
- Each edit replaces one exact snippet: copy `search` from the file character for character, including indentation, and make it long enough to appear only once.
- If excerpts from changelogs are included, use them to understand what changed. Cite an excerpt in sources only if your fix follows something it says, with that sentence copied word for word as the quote. Citations are checked against the excerpts, and an empty list is normal.

Respond with JSON only:
{"edits": [{"file": "path/in/repo.py", "search": "exact text", "replace": "new text"}], "rationale": "one sentence on what broke and why the fix is right", "sources": [{"url": "...", "quote": "the sentence you followed"}]}"""

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs")
SOURCE_BUDGET = 40_000  # characters of source sent per request


def source_files(folder: Path, test_output: str) -> dict[str, str]:
    """The repo's source files (not tests), the ones named in the failure first, within a size budget."""
    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix in SOURCE_SUFFIXES and "__pycache__" not in p.parts]
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
    edits = [
        Edit(str(e["file"]), str(e["search"]), str(e.get("replace", "")))
        for e in data.get("edits", []) if isinstance(e, dict) and e.get("file") and e.get("search")
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
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        if meter is not None:
            meter.record(model, response.usage, time.perf_counter() - start)
        edits, rationale, cited = parse_fix(response.choices[0].message.content or "")
        return edits, rationale, verified(cited, research)  # only quotes that are really on the pages

    return fix
