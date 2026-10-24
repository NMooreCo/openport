"""The quality gate: run a repo's evals on the models its code names now, and fail if any call drops below the bar.

`openport port` records the bar and the eval spec's location in `openport.yaml`, and adds a GitHub
Action that runs `openport check .` on every pull request.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import tokenfactory
from .evals.runner import SiteResult, make_judge, run_site
from .evals.spec import Spec, find_spec, load_spec

DEFAULT_MIN_PASS_RATE = 0.9
SUMMARY_FILE = "openport.yaml"


@dataclass
class GateResult:
    site: SiteResult
    required: float

    @property
    def ok(self) -> bool:
        return self.site.pass_rate >= self.required


def gate_settings(root: Path) -> dict:
    summary = root / SUMMARY_FILE
    if not summary.exists():
        return {}
    return (yaml.safe_load(summary.read_text(encoding="utf-8")) or {}).get("gate") or {}


def load_gate_spec(root: Path) -> Spec:
    settings = gate_settings(root)
    return load_spec(root / settings["evals"]) if settings.get("evals") else load_spec(find_spec(root))


def run_gate(
    root: Path,
    *,
    min_pass_rate: float | None = None,
    sites: list[str] | None = None,
    concurrency: int = 8,
    use_judge: bool = True,
    client=None,
) -> list[GateResult]:
    spec = load_gate_spec(root)
    required = min_pass_rate if min_pass_rate is not None else gate_settings(root).get("min_pass_rate", DEFAULT_MIN_PASS_RATE)
    client = client or tokenfactory.client()
    judge = make_judge(client=client) if use_judge else None
    return [
        GateResult(run_site(spec, site, "as written", None, concurrency=concurrency, judge=judge, client=client), required)
        for site in spec.call_sites
        if not sites or site.id in sites
    ]


def workflow(install_app: str | None, openport_source: str) -> str:
    """A GitHub Actions workflow that runs the gate on pull requests."""
    install = f"          {install_app}\n" if install_app else ""
    return f"""# Added by OpenPort. On every pull request, re-runs each LLM call's evals on the NVIDIA Nemotron
# model its code names, and fails if any call drops below the bar in openport.yaml.
# Needs a NEBIUS_API_KEY repository secret (Settings > Secrets and variables > Actions).
name: OpenPort quality gate

on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  llm-evals:
    runs-on: ubuntu-latest
    # Pull requests from forks can't read repository secrets.
    if: github.event_name == 'workflow_dispatch' || github.event.pull_request.head.repo.full_name == github.repository
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Install the app and OpenPort
        run: |
          uv venv
{install}          uv pip install "openport @ {openport_source}"
      - name: Run each LLM call's evals on Nemotron
        env:
          NEBIUS_API_KEY: ${{{{ secrets.NEBIUS_API_KEY }}}}
        run: .venv/bin/openport check .
"""


def install_command(root: Path) -> str | None:
    if (root / "pyproject.toml").exists():
        return "uv pip install -r pyproject.toml"
    if (root / "requirements.txt").exists():
        return "uv pip install -r requirements.txt"
    return None
