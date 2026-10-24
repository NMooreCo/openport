"""Read recorded calls (a JSONL log) into eval cases, and guess what a field grader should check.

A log line is one recorded call: at least an `input` (the keyword arguments the call was made with),
and the result under `output` or `expected`. A recorded output becomes the reference the Nemotron
output is graded against, so the eval measures "does the open model still do what the closed one did".
"""

import json
from pathlib import Path


def load_log_cases(path: Path, site_id: str) -> list[dict]:
    cases: list[dict] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path.name}:{i} isn't valid JSON: {error}") from error
        if not isinstance(row, dict) or "input" not in row:
            raise ValueError(f"{path.name}:{i} needs an 'input' field")
        expected = row["expected"] if "expected" in row else row.get("output", {})
        case_id = str(row.get("id") or f"{site_id}-{i:02d}")
        cases.append({"id": case_id, "input": row["input"], "expected": expected})
    return cases


def infer_fields(cases: list[dict]) -> list[str]:
    """The keys every recorded output shares, in first-seen order: a starting point for a field grader."""
    dicts = [c["expected"] for c in cases if isinstance(c.get("expected"), dict) and c["expected"]]
    if not dicts:
        return []
    common = set(dicts[0]).intersection(*(set(d) for d in dicts[1:]))
    return [key for key in dicts[0] if key in common]
