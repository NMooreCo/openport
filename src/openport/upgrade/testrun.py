"""Run a repo's own test suite in a throwaway container, before and after an upgrade.

Fetched code is only ever run inside a container with just the upgraded copy mounted, never on the
host. The command suits the project: pip and pytest for a requirements file, uv for a uv project,
npm for a Node project with a test script. Pass another image and command for other stacks.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PYTHON_IMAGE = "python:3.12.11-slim"
NODE_IMAGE = "node:22.23.4-slim"
UV_INSTALL = "pip install -q --disable-pip-version-check --root-user-action=ignore uv==0.12.18"
PYTHON_TESTS = (
    "pip install -q --disable-pip-version-check --root-user-action=ignore -r requirements.txt pytest"
    " && python -m pytest -q -p no:cacheprovider"
)
UV_TESTS = f"{UV_INSTALL} && uv sync --frozen -q && uv run --frozen --with pytest pytest -q -p no:cacheprovider"
NODE_TESTS = "npm ci --no-audit --no-fund && npm test"
_SUMMARY = re.compile(r"\d+ (?:passed|failed|errors?)\b|no tests ran|error", re.IGNORECASE)
_NODE_COUNT = re.compile(r"^(?:#|ℹ)\s*(pass|fail)\s+(\d+)\s*$")  # node --test: "# pass 5" (TAP) or "ℹ pass 5" (spec)


@dataclass
class TestRun:
    ok: bool
    exit_code: int
    summary: str  # the runner's summary, e.g. pytest's "4 failed, 1 passed in 0.21s"
    output: str  # the tail of the output
    seconds: float

    __test__ = False  # not a pytest test class


def docker_command(folder: Path, image: str, command: str) -> list[str]:
    return [
        "docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{Path(folder).resolve()}:/repo", "-w", "/repo", image, "sh", "-c", command,
    ]


def test_setup(folder: Path, ecosystems: set[str]) -> tuple[str, str]:
    """The image and command that run this project's own tests, for the ecosystems being upgraded."""
    package = Path(folder) / "package.json"
    if "npm" in ecosystems and package.is_file():
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        if scripts.get("test"):
            return NODE_IMAGE, NODE_TESTS
    if (Path(folder) / "uv.lock").is_file() and not any(Path(folder).glob("requirements*.txt")):
        return PYTHON_IMAGE, UV_TESTS
    return PYTHON_IMAGE, PYTHON_TESTS


def summarize(output: str) -> str:
    """The test runner's own summary line, or one built from node --test's pass and fail counts."""
    lines = output.strip().splitlines()
    counts = {m[1]: int(m[2]) for m in (_NODE_COUNT.match(line.strip()) for line in lines) if m}
    if counts:
        return ", ".join(f"{counts[k]} {word}" for k, word in (("fail", "failed"), ("pass", "passed")) if counts.get(k))
    return next((line.strip(" =") for line in reversed(lines) if _SUMMARY.search(line)), lines[-1] if lines else "")


def run_in_container(folder: Path, image: str, command: str, *, run=subprocess.run, timeout: int = 900) -> TestRun:
    start = time.perf_counter()
    try:
        process = run(docker_command(folder, image, command), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        output, code = (process.stdout or "") + (process.stderr or ""), process.returncode
    except subprocess.TimeoutExpired:
        output, code = f"timed out after {timeout}s", -1
    lines = output.strip().splitlines()
    return TestRun(code == 0, code, summarize(output), "\n".join(lines[-80:]), round(time.perf_counter() - start, 1))


def run_tests(folder: Path, *, image: str = PYTHON_IMAGE, command: str = PYTHON_TESTS, run=subprocess.run, timeout: int = 900) -> TestRun:
    return run_in_container(folder, image, command, run=run, timeout=timeout)
