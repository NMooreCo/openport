"""Eval specs: which LLM call sites a target app has, and how to test and grade each one."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SPEC_FILE = "openport.evals.yaml"


@dataclass
class CallSite:
    id: str
    title: str
    file: str
    module: str
    function: str
    client_attr: str
    sdk: str  # "openai" or "anthropic"
    original_model: str
    output: str
    dataset: Path
    grader: dict = field(default_factory=dict)
    # Runs per case. Free text scored by a judge is noisy, so one run per case can fail a call on a single
    # unlucky draw; more samples give a steadier pass rate (every sample counts), not an easier bar.
    samples: int = 1

    def cases(self, limit: int | None = None) -> list[dict]:
        lines = self.dataset.read_text(encoding="utf-8").splitlines()
        cases = [json.loads(line) for line in lines if line.strip()]
        return cases[:limit] if limit else cases


@dataclass
class Spec:
    target: str
    source_root: Path
    call_sites: list[CallSite]


def find_spec(target: Path) -> Path:
    """Accept the spec file itself, the target's root, or its evals/ folder."""
    for candidate in (target, target / SPEC_FILE, target / "evals" / SPEC_FILE):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No {SPEC_FILE} found in {target} or {target / 'evals'}")


def load_spec(target: Path) -> Spec:
    path = find_spec(Path(target))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = path.parent
    sites = [
        CallSite(
            id=site["id"],
            title=site.get("title", site["id"]),
            file=site["file"],
            module=site["module"],
            function=site["function"],
            client_attr=site.get("client_attr", "client"),
            sdk=site["sdk"],
            original_model=site["original_model"],
            output=site.get("output", "text"),
            dataset=base / site["dataset"],
            grader=site.get("grader", {}),
            samples=max(1, int(site.get("samples", 1))),
        )
        for site in raw["call_sites"]
    ]
    return Spec(target=raw["target"], source_root=(base / raw.get("source_root", ".")).resolve(), call_sites=sites)
