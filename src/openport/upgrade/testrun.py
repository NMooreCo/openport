"""Run a repo's own test suite in a throwaway container, before and after an upgrade.

Fetched code is only ever run inside a container with just a scratch copy of the upgrade mounted,
never on the host. The command suits the project: pip and pytest for a requirements file, uv or Poetry
for their projects, npm or pnpm for a Node project with a test script; a repo upgraded in both
ecosystems runs both suites, and every one must pass. Pass another image and command for other stacks.
"""

import json
import os
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
# --locked fails if uv.lock is stale for pyproject.toml, as `uv sync --locked` in the repo's own CI would.
UV_TESTS = f"{UV_INSTALL} && uv sync --locked -q && uv run --locked --with pytest pytest -q -p no:cacheprovider"
POETRY_INSTALL = "pip install -q --disable-pip-version-check --root-user-action=ignore poetry==2.5.1"  # 2.4 added solver.min-release-age
# `poetry install` refuses a poetry.lock that's stale for pyproject.toml, as the repo's own CI would.
POETRY_TESTS = (
    f"{POETRY_INSTALL} && poetry install --no-interaction -q"
    " && poetry run python -m pip install -q --disable-pip-version-check pytest"
    " && poetry run python -m pytest -q -p no:cacheprovider"
)
NODE_TESTS = "npm ci --no-audit --no-fund && npm test"
NO_NODE_TESTS = "echo 'package.json has no test script, so the npm upgrades can't be proven' && exit 1"
NO_PNPM = "echo \"error: can't tell which pnpm wrote pnpm-lock.yaml\" && exit 1"
PNPM_VERSION = "10.34.7"  # for a repo that doesn't name its pnpm; 10.16 added minimumReleaseAge
PNPM_FOR_LOCKFILE = {"9": PNPM_VERSION, "6": "8.15.9"}  # lockfileVersion's major -> a pnpm that writes that format
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
    # The installs go before the container exits: made in Linux, they hold symlinks Windows can't delete
    # (uv's .venv/lib64, npm's node_modules/.bin), and on a Linux host they belong to root. On Linux, hand
    # what's left back to the folder's owner too (Docker Desktop maps ownership already).
    # A monorepo's project runs from its own folder (`cd backend && ...`), so its installs are cleared from there too.
    owner = '; chown -R "$(stat -c %u:%g /repo)" /repo 2>/dev/null' if os.name != "nt" else ""
    command = f"{command}; status=$?; rm -rf /repo/node_modules /repo/.venv node_modules .venv{owner}; exit $status"
    return [
        "docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{Path(folder).resolve()}:/repo", "-w", "/repo", image, "sh", "-c", command,
    ]


def pnpm_version(folder: Path) -> str | None:
    """The pnpm a repo uses: the one its package.json names, else one that writes its lockfile's format."""
    package = Path(folder) / "package.json"
    manager = json.loads(package.read_text(encoding="utf-8")).get("packageManager") if package.is_file() else None
    named = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(?:\+\S*)?", str(manager or ""))
    if named:
        return named[1]
    lock = Path(folder) / "pnpm-lock.yaml"
    found = re.search(r"^lockfileVersion:\s*['\"]?(\d+)", lock.read_text(encoding="utf-8", errors="replace"), re.M) if lock.is_file() else None
    return PNPM_FOR_LOCKFILE.get(found[1]) if found else None


def pnpm_install(version: str) -> str:
    return f"npm install -g --no-audit --no-fund --loglevel=error pnpm@{version}"


def test_setups(folder: Path, ecosystems: set[str]) -> list[tuple[str, str, str]]:
    """(name, image, command) for each test suite the upgraded ecosystems need: npm's or pnpm's for npm
    packages, Python's for PyPI ones. A Node project without a test script can't prove its upgrade."""
    folder = Path(folder)
    setups = []
    if "npm" in ecosystems:
        package = folder / "package.json"
        scripts = (json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}) if package.is_file() else {}
        pnpm = (folder / "pnpm-lock.yaml").is_file() and not (folder / "package-lock.json").is_file()
        version = pnpm_version(folder) if pnpm else None
        if not scripts.get("test"):
            command = NO_NODE_TESTS
        elif pnpm:  # --frozen-lockfile fails if pnpm-lock.yaml is stale for package.json, as in CI
            command = f"{pnpm_install(version)} && pnpm install --frozen-lockfile && pnpm test" if version else NO_PNPM
        else:
            command = NODE_TESTS
        setups.append(("pnpm test" if pnpm else "npm test", NODE_IMAGE, command))
    if "PyPI" in ecosystems or not setups:
        pinned = any(folder.glob("requirements*.txt"))
        if not pinned and (folder / "poetry.lock").is_file():
            command = POETRY_TESTS
        elif not pinned and (folder / "uv.lock").is_file():
            command = UV_TESTS
        else:
            command = PYTHON_TESTS
        setups.append(("pytest", PYTHON_IMAGE, command))
    return setups


def combine(runs: list[tuple[str, "TestRun"]]) -> "TestRun":
    """One result for several suites: passing only if every suite passed."""
    if len(runs) == 1:
        return runs[0][1]
    failed = next((r for _, r in runs if not r.ok), None)
    return TestRun(
        ok=failed is None,
        exit_code=failed.exit_code if failed else 0,
        summary="; ".join(f"{name}: {r.summary}" for name, r in runs),
        output="\n".join(f"== {name} ==\n{r.output}" for name, r in runs)[-12000:],
        seconds=round(sum(r.seconds for _, r in runs), 1),
    )


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


def run_suites(folder: Path, setups: list[tuple[str, str, str]], *, run_tests=run_tests) -> TestRun:
    """Run every suite in `setups` on `folder` and combine the results."""
    return combine([(name, run_tests(folder, image=image, command=command)) for name, image, command in setups])
