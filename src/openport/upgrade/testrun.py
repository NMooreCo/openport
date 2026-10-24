"""Run a repo's own test suite in a throwaway container, before and after an upgrade.

Fetched code is only ever run inside a container with just the upgraded copy mounted, never on the
host. The default suits Python repos with a requirements file and pytest; pass another image and
command for other stacks.
"""

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PYTHON_IMAGE = "python:3.12.11-slim"
PYTHON_TESTS = (
    "pip install -q --disable-pip-version-check --root-user-action=ignore -r requirements.txt pytest"
    " && python -m pytest -q -p no:cacheprovider"
)
_SUMMARY = re.compile(r"\d+ (?:passed|failed|errors?)\b|no tests ran|error", re.IGNORECASE)


@dataclass
class TestRun:
    ok: bool
    exit_code: int
    summary: str  # pytest's last line, e.g. "4 failed, 1 passed in 0.21s"
    output: str  # the tail of the output
    seconds: float

    __test__ = False  # not a pytest test class


def docker_command(folder: Path, image: str, command: str) -> list[str]:
    return [
        "docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{Path(folder).resolve()}:/repo", "-w", "/repo", image, "sh", "-c", command,
    ]


def run_tests(folder: Path, *, image: str = PYTHON_IMAGE, command: str = PYTHON_TESTS, run=subprocess.run, timeout: int = 900) -> TestRun:
    start = time.perf_counter()
    try:
        process = run(docker_command(folder, image, command), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        output, code = (process.stdout or "") + (process.stderr or ""), process.returncode
    except subprocess.TimeoutExpired:
        output, code = f"tests timed out after {timeout}s", -1
    lines = output.strip().splitlines()
    summary = next((line.strip(" =") for line in reversed(lines) if _SUMMARY.search(line)), lines[-1] if lines else "")
    return TestRun(code == 0, code, summary, "\n".join(lines[-80:]), round(time.perf_counter() - start, 1))
