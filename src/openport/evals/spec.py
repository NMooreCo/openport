"""Eval specs: which LLM call sites a target app has, and how to test and grade each one."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .node import is_node_site

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
    # The eval calls `entry` in `module` (default: `function`), which can be an adapter that turns plain JSON
    # inputs into the app's own types. `file` and `function` stay the real LLM call site, which the port edits.
    entry: str = ""
    # The module whose `client_attr` is swapped for Token Factory (default: `module`), for apps that share
    # one client from elsewhere.
    client_module: str = ""
    # TypeScript/JavaScript: `module` is a file, `entry` (or `function`) its export, and `args` the input keys
    # passed to it in order (default: the whole input as one argument). Python entries take keyword arguments.
    args: list[str] = field(default_factory=list)
    # No module attribute to swap (the code builds its client in a function, on `self`...): the eval routes the
    # SDK's own calls instead (evals/intercept.py), which it does for every Python site anyway.
    intercept: bool = False
    # A call in a class's method (`function: Assistant.reply`) runs on an instance built for each case: with no
    # arguments, or these (keyword arguments as a mapping, positional ones as a list). See evals/entry.py.
    init: dict | list | None = None

    @property
    def runs_on_node(self) -> bool:
        return is_node_site(self.module)

    @property
    def entry_function(self) -> str:
        return self.entry or self.function

    @property
    def client_home(self) -> str:
        return self.client_module or self.module

    def cases(self, limit: int | None = None) -> list[dict]:
        lines = self.dataset.read_text(encoding="utf-8").splitlines()
        cases = [json.loads(line) for line in lines if line.strip()]
        for number, case in enumerate(cases, start=1):  # every case needs an ID, as text: results are keyed by it
            case["id"] = str(case.get("id") or f"{self.id}-{number:02d}")
        return cases[:limit] if limit else cases


@dataclass
class Spec:
    target: str
    source_root: Path
    call_sites: list[CallSite]
    extra_paths: list[Path] = field(default_factory=list)  # also importable during evals, e.g. an adapter module


def _source_root(base: Path, value: str) -> Path:
    """A path relative to the spec, or `repo:owner/name` for a GitHub repo OpenPort fetches (and reuses)."""
    if value.startswith("repo:"):
        from ..workspace import fetch_repo

        root, _ = fetch_repo(value.removeprefix("repo:"))
        return root.resolve()
    return (base / value).resolve()


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
            module=site.get("module") or (site["file"] if is_node_site(site["file"]) else site["module"]),
            function=site["function"],
            client_attr=site.get("client_attr", "client"),
            sdk=site["sdk"],
            original_model=site["original_model"],
            output=site.get("output", "text"),
            dataset=base / site["dataset"],
            grader=site.get("grader", {}),
            samples=max(1, int(site.get("samples", 1))),
            entry=site.get("entry", ""),
            client_module=site.get("client_module", ""),
            args=list(site.get("args") or []),
            intercept=bool(site.get("intercept", False)),
            init=site.get("init"),
        )
        for site in raw["call_sites"]
    ]
    return Spec(
        target=raw["target"],
        source_root=_source_root(base, raw.get("source_root", ".")),
        call_sites=sites,
        extra_paths=[(base / p).resolve() for p in raw.get("extra_paths", [])],
    )
