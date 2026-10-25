"""Field study: run OpenPort's scanner (and, with --advisories, the upgrade dry run) over popular open-source AI apps.

Static only: repos are shallow-cloned and read, never run. Results go to runs/field-study/<owner>__<name>.json.
Usage: uv run python scripts/field_study.py [--advisories] owner/name ...
"""

import json
import re
import sys
import time
import traceback
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


def lockfiles(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in root.rglob("*"):
        parts = set(path.relative_to(root).parts[:-1])
        if path.is_file() and (path.name in LOCKFILES or re.fullmatch(r"requirements[^/]*\.txt", path.name)) and not parts & SKIP_DIRS:
            found.setdefault(path.name if not path.name.startswith("requirements") else "requirements*.txt", []).append(
                path.relative_to(root).as_posix())
    return found


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


def main(argv: list[str]) -> None:
    advisories = "--advisories" in argv
    targets = [a for a in argv if not a.startswith("--")]
    OUT.mkdir(parents=True, exist_ok=True)

    def one(target: str) -> None:
        name = target.replace("/", "__")
        try:
            result = study(target, advisories)
        except Exception as error:
            result = {"repo": target, "error": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-1500:]}
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        summary = result.get("error") or (f"{result['call_sites']} calls ({result['testable_as_is']} testable), "
                                          f"{result['files_scanned']} files, scan {result['seconds']['scan']}s"
                                          + (f", {result['upgrade']['vulnerable']} vulnerable deps" if "upgrade" in result else ""))
        print(f"{target}: {summary}", flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, targets))


if __name__ == "__main__":
    main(sys.argv[1:])
