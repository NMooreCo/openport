"""Run a repo's own test suite in a throwaway container, before and after an upgrade.

Fetched code is only ever run inside a container with just a scratch copy of the upgrade mounted,
never on the host. The command suits the project: pip and pytest for a requirements file, uv or Poetry
for their projects, npm, pnpm, Yarn or bun for a Node project with a test script; a repo upgraded in both
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
NO_YARN = "echo \"error: can't tell which yarn wrote yarn.lock\" && exit 1"
PNPM_VERSION = "10.34.7"  # for a repo that doesn't name its pnpm; 10.16 added minimumReleaseAge
PNPM_FOR_LOCKFILE = {"9": PNPM_VERSION, "6": "8.15.9"}  # lockfileVersion's major -> a pnpm that writes that format
# bun runs inside the Node image (installed from npm), so a test script that calls node still works. A bun.lock
# with "configVersion" was written by bun 1.3.2 or later; an older one gets a bun that keeps its format and still
# has --minimum-release-age (added in 1.3.0).
BUN_VERSION, BUN_OLD_FORMAT = "1.4.0", "1.3.1"
COREPACK = "export COREPACK_ENABLE_DOWNLOAD_PROMPT=0 && corepack enable"  # Yarn 2+ comes from the repo's packageManager
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


def _manager(folder: Path, name: str) -> str | None:
    """The version of `name` that package.json's packageManager names, if it does."""
    package = Path(folder) / "package.json"
    manager = json.loads(package.read_text(encoding="utf-8")).get("packageManager") if package.is_file() else None
    named = re.fullmatch(rf"{name}@(\d+\.\d+\.\d+)(?:\+\S*)?", str(manager or ""))
    return named[1] if named else None


def bun_version(folder: Path) -> str:
    """The bun a repo uses: the one its package.json names, else one that keeps its bun.lock's format."""
    lock = Path(folder) / "bun.lock"
    newer = lock.is_file() and '"configVersion"' in lock.read_text(encoding="utf-8", errors="replace")
    return _manager(folder, "bun") or (BUN_VERSION if newer else BUN_OLD_FORMAT)


def bun_install(version: str) -> str:
    return f"npm install -g --no-audit --no-fund --no-update-notifier --loglevel=error bun@{version}"


def yarn_version(folder: Path) -> str | None:
    """The Yarn 2+ a repo uses: its package.json's packageManager, or the release .yarnrc.yml points at."""
    named = _manager(folder, "yarn")
    rc = Path(folder) / ".yarnrc.yml"
    path = re.search(r"^yarnPath:\s*['\"]?\S*?yarn-(\d+\.\d+\.\d+)\.c?js", rc.read_text(encoding="utf-8"), re.M) if rc.is_file() else None
    return named or (path[1] if path else None)


def yarn_berry(folder: Path) -> bool:
    lock = Path(folder) / "yarn.lock"
    return lock.is_file() and "__metadata:" in lock.read_text(encoding="utf-8", errors="replace")


def node_manager(folder: Path) -> str:
    """Which package manager installs a Node project: the one package.json names, else the one whose lockfile it
    has (npm's first), as the upgrade decides which lockfile is the project's own (lockfiles.node_lockfile_order)."""
    from .lockfiles import NODE_LOCKFILES, node_lockfile_order

    folder = Path(folder)
    managers = {lockfile: manager for manager, lockfile in NODE_LOCKFILES.items()}
    return next((managers[lockfile] for lockfile in node_lockfile_order(folder) if (folder / lockfile).is_file()), "npm")


def test_setups(folder: Path, ecosystems: set[str]) -> list[tuple[str, str, str]]:
    """(name, image, command) for each test suite the upgraded ecosystems need: npm's or pnpm's for npm
    packages, Python's for PyPI ones. A Node project without a test script can't prove its upgrade."""
    folder = Path(folder)
    setups = []
    if "npm" in ecosystems:
        package = folder / "package.json"
        scripts = (json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}) if package.is_file() else {}
        manager = node_manager(folder)
        # Each install refuses a lockfile that's stale for package.json, as the repo's own CI would.
        if not scripts.get("test"):
            command = NO_NODE_TESTS
        elif manager == "pnpm":
            version = pnpm_version(folder)
            command = f"{pnpm_install(version)} && pnpm install --frozen-lockfile && pnpm test" if version else NO_PNPM
        elif manager == "bun":
            command = f"{bun_install(bun_version(folder))} && bun install --frozen-lockfile && bun run test"
        elif manager == "yarn" and yarn_berry(folder):
            command = f"{COREPACK} && yarn install --immutable && yarn test" if yarn_version(folder) else NO_YARN
        elif manager == "yarn":  # Yarn 1 comes with the Node image
            command = "yarn install --frozen-lockfile --non-interactive && yarn test"
        else:
            command = NODE_TESTS
        setups.append((f"{manager} test", NODE_IMAGE, command))
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
