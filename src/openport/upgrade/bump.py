"""Bump locked dependencies the way their package manager would, in a throwaway container.

requirements.txt pins are edited in place (apply.py). uv.lock and package-lock.json are rewritten by
uv and npm themselves, run in a container with only the upgraded copy mounted: nothing is installed on
the host, and npm runs no install scripts. Both resolve only releases older than the cutoff, so the
supply-chain guard covers every package a bump pulls in, not just the one being upgraded. poetry.lock
and pnpm-lock.yaml still need their own tools.
"""

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .apply import _read, _write
from .testrun import NODE_IMAGE, PYTHON_IMAGE, UV_INSTALL

LOCKFILES = ("uv.lock", "package-lock.json")  # what OpenPort can bump, besides requirements.txt pins
NPM_LOCK_ONLY = "npm install --package-lock-only --ignore-scripts --no-audit --no-fund"


@dataclass
class BumpStep:
    lockfile: str
    image: str
    command: str
    packages: list[str]  # the packages this step moves


def uv_step(bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """`uv lock --upgrade-package NAME==VERSION`: works for transitive packages too, within the project's constraints.
    uv records --exclude-newer in uv.lock, which would make the repo's own `uv lock --check` fail; the plain
    `uv lock` after it drops the option and keeps every version just resolved."""
    flags = " ".join(f"--upgrade-package {shlex.quote(f'{name}=={version}')}" for name, version in bumps.items())
    command = f"{UV_INSTALL} && uv lock --exclude-newer {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')} {flags} && uv lock"
    return BumpStep("uv.lock", PYTHON_IMAGE, command, list(bumps))


def npm_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """Direct dependencies are installed at the new version; a transitive one gets an `overrides` entry in package.json."""
    path = folder / "package.json"
    text = _read(path)
    package = json.loads(text)
    direct = {name for group in ("dependencies", "devDependencies", "optionalDependencies") for name in package.get(group) or {}}
    installs = [f"{name}@{version}" for name, version in bumps.items() if name in direct]
    overrides = {name: version for name, version in bumps.items() if name not in direct}
    lock_only = f"{NPM_LOCK_ONLY} --before={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    commands = []
    if installs:
        commands.append(f"{lock_only} " + " ".join(shlex.quote(spec) for spec in installs))
    if overrides:
        package["overrides"] = {**(package.get("overrides") or {}), **overrides}
        indent = re.search(r"\n([ \t]+)\"", text)
        newline = "\r\n" if "\r\n" in text else "\n"
        written = json.dumps(package, indent=indent[1] if indent else 2, ensure_ascii=False) + "\n"
        _write(path, written.replace("\n", newline))
        commands.append(lock_only)
    return BumpStep("package-lock.json", NODE_IMAGE, " && ".join(commands), list(bumps))


def bump_steps(folder: Path, bumps: dict[str, dict[str, str]], cutoff: datetime) -> list[BumpStep]:
    """The steps for {lockfile: {package: version}}, resolving nothing released after `cutoff` (UTC).
    npm's step changes package.json in `folder` before it runs."""
    steps = []
    if bumps.get("uv.lock"):
        steps.append(uv_step(bumps["uv.lock"], cutoff))
    if bumps.get("package-lock.json"):
        steps.append(npm_step(folder, bumps["package-lock.json"], cutoff))
    return steps
