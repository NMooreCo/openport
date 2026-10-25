"""Field study: run OpenPort's scanner, its port (worked out, never written) and, with --advisories, the upgrade
dry run over popular open-source AI apps, then print what docs/FIELD-STUDY.md reports: the totals and the per-app
table.

Static only: repos are shallow-cloned and read, never run. Results go to runs/field-study/<owner>__<name>.json.
Usage: uv run python scripts/field_study.py [--advisories] [owner/name ...]   (no repos: the study's 20)
       uv run python scripts/field_study.py --summary                         (the report, from the saved results)
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openport import config
from openport.capture.draft import runnable, ts_entry_params
from openport.port.plan import build_plan
from openport.port.python_rewrite import port_python
from openport.port.ts_rewrite import port_typescript
from openport.scan import SKIP_DIRS, _source_files, scan_repo
from openport.workspace import fetch_repo

OUT = config.RUNS_DIR / "field-study"
LOCKFILES = ("requirements.txt", "uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb", "Pipfile.lock")
# How else an app reaches an LLM, for the repos where the SDK scan finds little.
OTHER = {
    "litellm": re.compile(r"^\s*(?:import litellm|from litellm\b)", re.M),
    "langchain": re.compile(r"langchain_(?:openai|anthropic)|@langchain/(?:openai|anthropic)|langchain\.chat_models|ChatOpenAI\(|ChatAnthropic\("),
    "vercel_ai_sdk": re.compile(r"""from\s+['"]@ai-sdk/(?:openai|anthropic)['"]|require\(['"]@ai-sdk/"""),
    "raw_http": re.compile(r"api\.openai\.com/v1|api\.anthropic\.com/v1|/v1/chat/completions['\"`]|/v1/messages['\"`]"),
}
OTHER_NAMES = {"litellm": "LiteLLM", "langchain": "LangChain", "vercel_ai_sdk": "Vercel AI SDK"}
# The 20 most-starred applications and end-user tools tagged openai, anthropic, claude or chatgpt, in Python or
# TypeScript (GitHub search, Oct 24, 2026; docs/FIELD-STUDY.md says what was left out and why).
REPOS = [
    "NousResearch/hermes-agent", "microsoft/markitdown", "Significant-Gravitas/AutoGPT", "langgenius/dify", "open-webui/open-webui",
    "langflow-ai/langflow", "paperclipai/paperclip", "thedotmack/claude-mem", "OpenHands/OpenHands", "ChatGPTNextWeb/NextChat",
    "lobehub/lobehub", "shareAI-lab/learn-claude-code", "ruvnet/ruflo", "headroomlabs-ai/headroom", "calesthio/OpenMontage",
    "binary-husky/gpt_academic", "code-yeongyu/oh-my-openagent", "Aider-AI/aider", "zhayujie/CowAgent", "LibreChat-AI/LibreChat",
]
# What each reason the port gives for leaving a call where it is comes down to: (group, label, words in the reason).
KEPT = [
    ("client", "handed it by their caller", "whatever its caller passes"),
    ("client", "handed it as their object is built", "whatever builds the object passes"),
    ("client", "a client of another class", "not one of OpenAI"),
    ("client", "set up by a class outside the app", "set up by the class it extends"),
    ("client", "not traced", "couldn't find where its client"),
    ("url", "URLs from settings or variables", "points: move it to"),
    ("provider", "a factory choosing between providers", "one of several, for different providers"),
    ("provider", "a client built from its caller's settings", "takes its provider from whoever builds it"),
    ("provider", "a table of endpoints", "picks its URL from several"),
    ("serve", "images", "Sends images"),
    ("serve", "Anthropic's own HTTP format", "Anthropic's Messages API"),
    ("serve", "embeddings", "Embeddings"),
    ("serve", "audio", "returns audio"),
    ("serve", "the provider's own web search", "Searches the web"),
    ("serve", "OpenAI's Responses API over HTTP", "Responses API"),
    ("serve", "a streamed Anthropic response", "Streams an Anthropic"),
    ("other", "a server on the same machine", "same machine"),
    ("other", "Azure OpenAI", "Azure"),
]


def lockfiles(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in root.rglob("*"):
        parts = set(path.relative_to(root).parts[:-1])
        if path.is_file() and (path.name in LOCKFILES or re.fullmatch(r"requirements[^/]*\.txt", path.name)) and not parts & SKIP_DIRS:
            found.setdefault(path.name if not path.name.startswith("requirements") else "requirements*.txt", []).append(
                path.relative_to(root).as_posix())
    return found


def why(reason: str) -> tuple[str, str]:
    """The (group, label) a reason the port gives for leaving a call where it is comes down to."""
    return next(((group, label) for group, label, words in KEPT if words in reason), ("other", reason[:80]))


def stars(target: str) -> int | None:
    """The repo's stars on GitHub now (the study's table records them as it runs)."""
    token = os.getenv("GITHUB_TOKEN")
    request = urllib.request.Request(f"https://api.github.com/repos/{target}",
                                     headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response).get("stargazers_count")
    except (OSError, ValueError):
        return None


def study(target: str, advisories: bool) -> dict:
    started = time.perf_counter()
    root, _ = fetch_repo(target)
    fetched = time.perf_counter()
    report = scan_repo(root)
    scanned = time.perf_counter()
    sites = report.portable()
    testable, reasons = 0, Counter()
    for site in sites:
        if site.language == "python":
            ok, why = runnable(site, root)
        else:  # dependencies aren't installed here, so ask only whether the harness could call it once they are
            ok, why = runnable(site, None)
            if ok and ts_entry_params(root, site) is None:
                ok, why = False, "not at the top of its module, where the harness can call it"
        testable += ok
        if not ok:
            reasons[re.sub(r"\(.*?\)|'.*?'", "…", why)] += 1
    # What `openport port` would do with each call: the rewriters only read the repo and return what they'd write.
    plan = build_plan(sites, {"nano": "nano", "super": "super", "ultra": "ultra"})
    port_python(root, [s for s in sites if s.language == "python"], plan, b"")
    port_typescript(root, [s for s in sites if s.language == "typescript"], plan)
    ported = Counter(s.sdk for s in sites if not plan[s.id].keep)
    kept = Counter(re.sub(r"\(.*?\)|`.*?`|'.*?'", "…", plan[s.id].reason) for s in sites if plan[s.id].keep)
    kept_why = Counter("|".join(why(plan[s.id].reason)) for s in sites if plan[s.id].keep)
    other = Counter()
    for path in _source_files(root):
        if path.stat().st_size > 500_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in OTHER.items():
            if pattern.search(text):
                other[name] += 1
    result = {
        "repo": target,
        "stars": stars(target),
        "files_scanned": report.files_scanned,
        "skipped_files": len(report.skipped),
        "call_sites": len(sites),
        "in_tests": sum(f.in_tests for f in report.findings),
        "by_sdk": dict(Counter(f.sdk for f in sites)),
        "by_language": dict(Counter(f.language for f in sites)),
        "by_api": dict(Counter(f"{f.sdk} {f.api}" for f in sites)),
        "by_output": dict(Counter(f.output for f in sites)),
        "by_portability": dict(Counter(f.portability for f in sites)),
        "testable_as_is": testable,
        "not_testable_because": dict(reasons),
        "ported": dict(ported),
        "kept_because": dict(kept),
        "kept_why": dict(kept_why),  # "group|label": count, from the reasons as the port gave them
        "other_llm_access_files": dict(other),
        "lockfiles": lockfiles(root),
        "seconds": {"fetch": round(fetched - started, 1), "scan": round(scanned - fetched, 1)},
    }
    if advisories:
        from openport.upgrade import plan_upgrades, read_lockfiles

        deps = read_lockfiles(root)
        began = time.perf_counter()
        plans = plan_upgrades(deps)
        result["upgrade"] = {
            "root_dependencies": len(deps),
            "vulnerable": len(plans),
            "advisories": sum(len(p.advisory_ids) for p in plans),
            "by_status": dict(Counter(p.status for p in plans)),
            "seconds": round(time.perf_counter() - began, 1),
        }
    return result


GROUPS = {  # how the report groups the reasons calls stay where they are
    "client": "get their client from code the port doesn't rewrite",
    "url": "are plain HTTP calls whose URL comes from settings or variables",
    "provider": "pick their provider at runtime",
    "serve": "do something Nemotron on Token Factory can't serve as written",
    "other": "stay for other reasons",
}


def _kept(result: dict) -> Counter:
    if "kept_why" in result:
        return Counter({tuple(key.split("|", 1)): count for key, count in result["kept_why"].items()})
    found: Counter = Counter()  # results saved before kept_why: from the reasons as recorded
    for reason, count in (result.get("kept_because") or {}).items():
        found[why(reason)] += count
    return found


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "–"


def summary(results: list[dict]) -> str:
    """What docs/FIELD-STUDY.md reports, from the study's results: the totals, and the per-app table."""
    done = [r for r in results if "error" not in r]
    count = lambda field, key: sum((r.get(field) or {}).get(key, 0) for r in done)  # noqa: E731
    calls, testable = sum(r["call_sites"] for r in done), sum(r["testable_as_is"] for r in done)
    openai, anthropic, http = (count("by_sdk", k) for k in ("openai", "anthropic", "http"))
    moved = sum(sum((r.get("ported") or {}).values()) for r in done)
    lines = [
        f"- {calls} LLM calls in {sum(r['call_sites'] > 0 for r in done)} of the {len(done)} apps' own code: {openai + anthropic} through "
        f"the SDKs ({openai} OpenAI, {anthropic} Anthropic) in {sum(r['by_sdk'].get('openai', 0) + r['by_sdk'].get('anthropic', 0) > 0 for r in done)} "
        f"apps, and {http} over plain HTTP in {sum(r['by_sdk'].get('http', 0) > 0 for r in done)}. {count('by_language', 'python')} are in Python and "
        f"{count('by_language', 'typescript')} in TypeScript; {count('by_output', 'tools')} use tool calling. Another "
        f"{sum(r['in_tests'] for r in done)} sit in tests, docs' samples, benchmarks and evals.",
        f"- {testable} of the {calls} ({_share(testable, calls)}) can be tested as written.",
        f"- The port moves {moved} ({_share(moved, calls)}) to Nemotron as they are: {count('ported', 'openai') + count('ported', 'anthropic')} "
        f"SDK calls and {count('ported', 'http')} HTTP calls. Of the other {calls - moved}:",
    ]
    kept: Counter = Counter()
    for r in done:
        kept.update(_kept(r))
    for group, heading in GROUPS.items():
        labels = [(label, n) for (g, label), n in kept.most_common() if g == group]
        if labels:  # a group with one reason needs no list
            listed = ": " + "; ".join(f"{label} ({n})" for label, n in labels) if len(labels) > 1 else ""
            lines.append(f"  - {sum(n for _, n in labels)} {heading}{listed}")
    upgrades = [r["upgrade"] for r in done if "upgrade" in r]
    if upgrades:
        status: Counter = Counter()
        for u in upgrades:
            status.update(u["by_status"])
        vulnerable, fixable = sum(u["vulnerable"] for u in upgrades), status["upgrade"] + status["partial"]
        lines.append(
            f"- {sum(u['vulnerable'] > 0 for u in upgrades)} of the {len(upgrades)} lock a version with a known vulnerability: "
            f"{vulnerable:,} of {sum(u['root_dependencies'] for u in upgrades):,} locked versions, with {sum(u['advisories'] for u in upgrades):,} "
            f"advisories between them. {fixable} ({_share(fixable, vulnerable)}) have a fix at least a week old, {status['upgrade']} of them "
            f"fixing every one; {status['held: fix too new']} are held back because the only fix is newer than that, and "
            f"{status['no fix available']} have no fix yet.")
    table = ["| Repo | Stars | Files | SDK calls | HTTP calls | Testable as written | Moved by the port | Also reaches models via "
             "| Locked versions | Vulnerable | With a fix ≥ 7 days old |", "|---|---:|---:|---|---:|---:|---:|---|---:|---:|---:|"]
    order = {repo: position for position, repo in enumerate(REPOS)}
    for r in sorted(done, key=lambda r: (-(r.get("stars") or 0), order.get(r["repo"], len(order)))):
        sdk = " + ".join(f"{r['by_sdk'][k]} {name}" for k, name in (("openai", "OpenAI"), ("anthropic", "Anthropic")) if r["by_sdk"].get(k)) or "–"
        other = ", ".join(name for key, name in OTHER_NAMES.items() if (r.get("other_llm_access_files") or {}).get(key)) or "–"
        up = r.get("upgrade") or {}
        fixed = (up.get("by_status") or {}).get("upgrade", 0) + (up.get("by_status") or {}).get("partial", 0)
        cells = [f"[{r['repo']}](https://github.com/{r['repo']})", f"{r['stars']:,}" if r.get("stars") else "–", f"{r['files_scanned']:,}", sdk,
                 str(r["by_sdk"].get("http") or "–"), str(r["testable_as_is"]), str(sum((r.get("ported") or {}).values())), other,
                 *((f"{up['root_dependencies']:,}", str(up["vulnerable"]), str(fixed)) if up else ("–", "–", "–"))]
        table.append("| " + " | ".join(cells) + " |")
    return "\n".join(["## What I found", "", *lines, "", "## Per app", "", *table])


def main(argv: list[str]) -> None:
    saved = lambda: [json.loads(p.read_text(encoding="utf-8")) for p in sorted(OUT.glob("*.json"))]  # noqa: E731
    if "--summary" in argv:
        print(summary(saved()))
        return
    advisories = "--advisories" in argv
    targets = [a for a in argv if not a.startswith("--")] or REPOS
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def one(target: str) -> None:
        name = target.replace("/", "__")
        try:
            result = study(target, advisories)
        except Exception as error:
            result = {"repo": target, "error": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-1500:]}
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        results.append(result)
        line = result.get("error") or (f"{result['call_sites']} calls ({result['testable_as_is']} testable), "
                                       f"{result['files_scanned']} files, scan {result['seconds']['scan']}s"
                                       + (f", {result['upgrade']['vulnerable']} vulnerable deps" if "upgrade" in result else ""))
        print(f"{target}: {line}", flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, targets))
    print("\n" + summary(results))


if __name__ == "__main__":
    main(sys.argv[1:])
