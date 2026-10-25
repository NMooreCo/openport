"""Bump locked dependencies the way their package manager would, in a throwaway container.

requirements.txt pins are edited in place (apply.py). uv.lock, poetry.lock, package-lock.json,
pnpm-lock.yaml, yarn.lock and bun.lock are rewritten by uv, Poetry, npm, pnpm, Yarn and bun themselves,
run in a container with only a scratch copy of the upgrade mounted: nothing is installed on the host, and
the Node package managers run no install scripts. Each resolves only releases older than the cutoff (uv's
--exclude-newer, npm's --before, Poetry's solver.min-release-age, pnpm's minimumReleaseAge, Yarn's
npmMinimalAgeGate, bun's --minimum-release-age), so the supply-chain guard covers every package a bump pulls
in, not just the one being upgraded. Before a new lockfile is kept, every version it adds is checked against
the registry's release dates too (`too_new`), which also covers the versions of each tool that lack a gate
(pnpm before 10.16, Yarn 1 and Yarn before 4.10, bun before 1.3).
"""

import json
import re
import shlex
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .apply import _read
from .lockfiles import Dependency, parse_bun_lock, parse_package_lock, parse_pnpm_lock, parse_toml_lock, parse_yarn_lock
from .testrun import (COREPACK, NODE_IMAGE, POETRY_INSTALL, PYTHON_IMAGE, UV_INSTALL, bun_install, bun_version, pnpm_install,
                      pnpm_version, yarn_berry, yarn_version)

# What OpenPort can bump, besides requirements.txt pins.
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock")
NPM_LOCK_ONLY = "npm install --package-lock-only --ignore-scripts --no-audit --no-fund"
PARSERS = {
    "uv.lock": lambda text: parse_toml_lock(text, "uv.lock"),
    "poetry.lock": lambda text: parse_toml_lock(text, "poetry.lock"),
    "package-lock.json": parse_package_lock,
    "pnpm-lock.yaml": parse_pnpm_lock,
    "yarn.lock": parse_yarn_lock,
    "bun.lock": parse_bun_lock,
}

# Run with node in the container: give package.json's direct dependencies their ranges back ({name: "^9.0.0"}),
# after the package manager added them at their exact new version. Its own indentation stays.
_SET_RANGES = '''const fs = require("fs");
const ranges = JSON.parse(process.argv[1]);
const text = fs.readFileSync("package.json", "utf8");
const pkg = JSON.parse(text);
for (const group of ["dependencies", "devDependencies", "optionalDependencies"])
  for (const [name, range] of Object.entries(ranges)) if (name in (pkg[group] || {})) pkg[group][name] = range;
const indent = (text.match(/\\n([ \\t]+)"/) || [null, "  "])[1];
fs.writeFileSync("package.json", JSON.stringify(pkg, null, indent) + "\\n");
'''

# Run with node: in a Yarn 1 yarn.lock, the entry that `name@9.0.0` resolved to answers `name@^9.0.0` instead,
# which is what Yarn would have written had it kept 9.0.0 for the range (it re-resolves a new range to the
# newest release instead). Keys are quoted the way Yarn quotes them.
_YARN1_PATTERNS = '''const fs = require("fs");
const swaps = JSON.parse(process.argv[1]);
const quote = (key) => /^(true|false)|[:\\s\\\\",\\[\\]]|^[0-9]|^[^a-zA-Z]/.test(key) ? JSON.stringify(key) : key;
const lines = fs.readFileSync("yarn.lock", "utf8").split("\\n");
for (let i = 0; i < lines.length; i++) {
  const cr = lines[i].endsWith("\\r") ? "\\r" : "";
  const line = cr ? lines[i].slice(0, -1) : lines[i];
  if (!line || /^[\\s#]/.test(line) || !line.endsWith(":")) continue;
  const keys = line.slice(0, -1).split(", ").map((key) => key.replace(/^"(.*)"$/, "$1"));
  if (!keys.some((key) => key in swaps)) continue;
  lines[i] = [...new Set(keys.map((key) => swaps[key] ?? key))].sort().map(quote).join(", ") + ":" + cr;
}
fs.writeFileSync("yarn.lock", lines.join("\\n"));
'''

# Runs in the container with Poetry installed. Poetry can't move one locked package to a given version, so
# each is pinned with `poetry add --lock` first; then pyproject.toml goes back as it was and `poetry lock`
# keeps the new versions wherever the project's own constraints allow them. The others stay pinned, in their
# constraint's own style where it has one: ^1.7.1 becomes ^2.14.0, which the lock already satisfies.
_POETRY_BUMP = '''import json, re, subprocess, sys, tomllib

bumps, groups = json.loads(sys.argv[1]), json.loads(sys.argv[2])
norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()


def poetry(*args):
    if subprocess.run(["poetry", *args, "--no-interaction"]).returncode:
        sys.exit(f"error: poetry {args[0]} failed")


def pin(names):  # each at exactly its new version; a transitive package becomes a direct dependency
    by_group = {}
    for name in names:
        by_group.setdefault(groups.get(norm(name), "main"), []).append(f"{name}=={bumps[name]}")
    for group, specs in by_group.items():
        poetry("add", "--lock", *([] if group == "main" else ["--group", group]), *specs)


def off():  # the packages poetry.lock doesn't have at their new version
    with open("poetry.lock", "rb") as lock:
        got = {norm(p["name"]): p["version"] for p in tomllib.load(lock).get("package", [])}
    return [name for name, version in bumps.items() if got.get(norm(name)) != version]


def restyle(names):  # a pin that replaced ^1.7.1 or ~1.7 goes back to that style
    import tomlkit  # Poetry's own dependency, so it's here

    before = tomllib.loads(original.decode("utf-8"))
    with open("pyproject.toml", encoding="utf-8", newline="") as handle:
        doc = tomlkit.parse(handle.read())
    poetry_ = before.get("tool", {}).get("poetry", {})
    paths = [["dependencies"], ["dev-dependencies"], *(["group", g, "dependencies"] for g in poetry_.get("group", {}))]
    for path in paths:
        old, new = poetry_, doc.get("tool", {}).get("poetry", {})
        for key in path:
            old, new = old.get(key, {}), new.get(key, {})
        for key, value in old.items():
            spec = value.get("version") if isinstance(value, dict) else value
            name = next((n for n in names if norm(n) == norm(key)), None)
            if name and isinstance(spec, str) and spec[:1] in "^~" and not spec.startswith("~=") and key in new:
                if isinstance(new[key], str):
                    new[key] = spec[0] + bumps[name]
                else:
                    new[key]["version"] = spec[0] + bumps[name]
    with open("pyproject.toml", "w", encoding="utf-8", newline="") as handle:
        handle.write(tomlkit.dumps(doc))


with open("pyproject.toml", "rb") as handle:
    original = handle.read()
pin(bumps)
with open("pyproject.toml", "wb") as handle:
    handle.write(original)
poetry("lock")
pinned = off()
if pinned:
    pin(pinned)
    restyle(pinned)
    poetry("lock")
if off():
    sys.exit("error: poetry.lock didn't take " + ", ".join(off()))
'''


@dataclass
class BumpStep:
    lockfile: str
    image: str
    command: str
    packages: list[str]  # the packages this step moves
    files: tuple[str, ...] = ()  # other files the step may change; they come back with the lockfile
    writes: dict[str, str] = field(default_factory=dict)  # edits made in the scratch copy before it runs (overrides)


def _age(cutoff: datetime, unit_seconds: int) -> int:
    """The age guard in days or minutes, for package managers that take it that way."""
    return max(0, round((datetime.now(timezone.utc) - cutoff).total_seconds() / unit_seconds))


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dump_like(data: dict, text: str) -> str:
    """JSON with the original file's indentation and line endings."""
    indent = re.search(r"\n([ \t]+)\"", text)
    newline = "\r\n" if "\r\n" in text else "\n"
    return (json.dumps(data, indent=indent[1] if indent else 2, ensure_ascii=False) + "\n").replace("\n", newline)


def _failing(lockfile: str, image: str, message: str, bumps: dict[str, str]) -> BumpStep:
    """A step that only reports why the lockfile can't be bumped, so the run skips its packages with that reason."""
    return BumpStep(lockfile, image, f"echo {shlex.quote(message)} && exit 1", list(bumps))


def uv_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """`uv lock --upgrade-package NAME==VERSION`: works for transitive packages too, within the project's constraints.
    uv records --exclude-newer in uv.lock, which would make the repo's own `uv lock --check` fail; the plain
    `uv lock` after it drops the option and keeps every version just resolved."""
    flags = " ".join(f"--upgrade-package {shlex.quote(f'{name}=={version}')}" for name, version in bumps.items())
    command = f"{UV_INSTALL} && uv lock --exclude-newer {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')} {flags} && uv lock"
    return BumpStep("uv.lock", PYTHON_IMAGE, command, list(bumps))


def poetry_groups(pyproject: dict) -> dict[str, str]:
    """Each directly declared package's Poetry group: "main", or the group's name."""
    poetry = (pyproject.get("tool") or {}).get("poetry") or {}
    tables = [("main", poetry.get("dependencies") or {}),
              *((name, (group or {}).get("dependencies") or {}) for name, group in (poetry.get("group") or {}).items()),
              ("dev", poetry.get("dev-dependencies") or {})]
    groups: dict[str, str] = {}
    for group, table in tables:
        for name in table:
            groups.setdefault(_canonical(name), group)
    for spec in (pyproject.get("project") or {}).get("dependencies") or []:  # PEP 621
        groups.setdefault(_canonical(re.split(r"[\s\[<>=!~;(@]", spec, maxsplit=1)[0]), "main")
    groups.pop("python", None)
    return groups


def poetry_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """Each package lands at exactly its new version; pyproject.toml changes only where its constraint can't hold it."""
    path = Path(folder) / "pyproject.toml"
    groups = poetry_groups(tomllib.loads(_read(path))) if path.is_file() else {}
    command = (f"{POETRY_INSTALL} && export POETRY_SOLVER_MIN_RELEASE_AGE={_age(cutoff, 86400)}"
               f" && python -c {shlex.quote(_POETRY_BUMP)} {shlex.quote(json.dumps(bumps))} {shlex.quote(json.dumps(groups))}")
    return BumpStep("poetry.lock", PYTHON_IMAGE, command, list(bumps), ("pyproject.toml",))


def _direct(package: dict) -> dict[str, str]:
    """Each direct dependency's group in package.json."""
    groups: dict[str, str] = {}
    for group in ("dependencies", "devDependencies", "optionalDependencies"):
        for name in package.get(group) or {}:
            groups.setdefault(name, group)
    return groups


def npm_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """Direct dependencies are installed at the new version; a transitive one gets an `overrides` entry in package.json."""
    text = _read(Path(folder) / "package.json")
    package = json.loads(text)
    direct = _direct(package)
    installs = [f"{name}@{version}" for name, version in bumps.items() if name in direct]
    overrides = {name: version for name, version in bumps.items() if name not in direct}
    lock_only = f"{NPM_LOCK_ONLY} --before={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    commands, writes = [], {}
    if installs:
        commands.append(f"{lock_only} " + " ".join(shlex.quote(spec) for spec in installs))
    if overrides:
        package["overrides"] = {**(package.get("overrides") or {}), **overrides}
        writes["package.json"] = _dump_like(package, text)
        commands.append(lock_only)
    return BumpStep("package-lock.json", NODE_IMAGE, " && ".join(commands), list(bumps), ("package.json",), writes)


def _yaml_overrides(text: str, overrides: dict[str, str]) -> str | None:
    """pnpm-workspace.yaml with the overrides added to its own, or None if it already overrides one of them."""
    existing = (yaml.safe_load(text) or {}).get("overrides")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"overrides:\s*$", line)), None)
    if existing is not None and (start is None or not isinstance(existing, dict) or any(name in existing for name in overrides)):
        return None  # written inline, or already overriding one of them: left to a person
    following = re.match(r"([ \t]+)\S", lines[start + 1]) if start is not None and start + 1 < len(lines) else None
    indent = following[1] if following else "  "
    entries = "".join(f"{indent}{json.dumps(name)}: {json.dumps(version)}{newline}" for name, version in overrides.items())
    if start is not None:
        return "".join(lines[: start + 1]) + entries + "".join(lines[start + 1 :])
    return text + ("" if not text or text.endswith("\n") else newline) + f"overrides:{newline}" + entries


def pnpm_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """`pnpm add` for direct dependencies and an override for transitive ones, run with the repo's own pnpm (its
    package.json `packageManager`, else one that writes its lockfile's format). pnpm 10.16 and later hold back
    new releases themselves; for older ones the release-date check after the step is the guard."""
    folder = Path(folder)
    version = pnpm_version(folder)
    if version is None:
        return _failing("pnpm-lock.yaml", NODE_IMAGE, "error: can't tell which pnpm wrote pnpm-lock.yaml", bumps)
    text = _read(folder / "package.json")
    package = json.loads(text)
    direct = _direct(package)
    overrides = {name: target for name, target in bumps.items() if name not in direct}
    workspace = folder / "pnpm-workspace.yaml"
    writes = {}
    if overrides and int(version.split(".")[0]) >= 11:  # pnpm 11 keeps its settings in pnpm-workspace.yaml
        updated = _yaml_overrides(_read(workspace), overrides) if workspace.is_file() else None
        if updated is None:
            return _failing("pnpm-lock.yaml", NODE_IMAGE, f"error: add an override for {', '.join(overrides)} to pnpm-workspace.yaml by hand", bumps)
        writes["pnpm-workspace.yaml"] = updated
    elif overrides:
        settings = package.setdefault("pnpm", {})
        settings["overrides"] = {**(settings.get("overrides") or {}), **overrides}
        writes["package.json"] = _dump_like(package, text)
    flags = f"--lockfile-only --ignore-scripts --config.minimum-release-age={_age(cutoff, 60)}"
    root = " -w" if workspace.is_file() else ""  # adding at a workspace's root needs saying so
    saves = {"dependencies": "", "devDependencies": " --save-dev", "optionalDependencies": " --save-optional"}
    adds: dict[str, list[str]] = {}
    for name, target in bumps.items():
        if name in direct:
            adds.setdefault(saves[direct[name]], []).append(f"{name}@{target}")
    commands = [pnpm_install(version)]
    commands += [f"pnpm add {flags}{save}{root} " + " ".join(shlex.quote(spec) for spec in specs) for save, specs in adds.items()]
    if not adds:
        commands.append(f"pnpm install {flags}")
    return BumpStep("pnpm-lock.yaml", NODE_IMAGE, " && ".join(commands), list(bumps), ("package.json", "pnpm-workspace.yaml"), writes)


def _at_least(version: str, *least: int) -> bool:
    return tuple(int(part) for part in version.split(".")[: len(least)]) >= least


def _ranges(package: dict, bumps: dict[str, str]) -> dict[str, str]:
    """The ranges direct dependencies go back to after an exact add: ^8.5.1 becomes ^9.0.0, ~1.2.0 becomes ~1.3.1."""
    ranges = {}
    for group in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (package.get(group) or {}).items():
            if name in bumps and isinstance(spec, str) and spec[:1] in ("^", "~"):
                ranges.setdefault(name, spec[0] + bumps[name])
    return ranges


def _node(script: str, data: dict) -> str:
    return f"node -e {shlex.quote(script)} {shlex.quote(json.dumps(data))}"


def _transitive(folder: Path, bumps: dict[str, str], field_name: str) -> tuple[dict, dict[str, str], dict[str, str]]:
    """(package.json, its direct dependencies' groups, the writes that pin the transitive bumps in `field_name`)."""
    text = _read(Path(folder) / "package.json")
    package = json.loads(text)
    direct = _direct(package)
    pins = {name: target for name, target in bumps.items() if name not in direct}
    writes = {}
    if pins:
        updated = {**package, field_name: {**(package.get(field_name) or {}), **pins}}
        writes["package.json"] = _dump_like(updated, text)
    return package, direct, writes


def bun_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """`bun add` at each direct dependency's exact new version, then its range back (bun keeps the locked version
    when it still satisfies the range); a transitive one gets an `overrides` entry. Run with the repo's own bun."""
    folder = Path(folder)
    version = bun_version(folder)
    package, direct, writes = _transitive(folder, bumps, "overrides")
    gate = f" --minimum-release-age={_age(cutoff, 1)}" if _at_least(version, 1, 3) else ""
    flags = f"--lockfile-only --ignore-scripts{gate}"
    saves = {"dependencies": "", "devDependencies": " --dev", "optionalDependencies": " --optional"}
    adds: dict[str, list[str]] = {}
    for name, target in bumps.items():
        if name in direct:
            adds.setdefault(saves[direct[name]], []).append(f"{name}@{target}")
    commands = [bun_install(version)]
    commands += [f"bun add {flags}{save} " + " ".join(shlex.quote(spec) for spec in specs) for save, specs in adds.items()]
    ranges = _ranges(package, bumps)
    if ranges:
        commands.append(_node(_SET_RANGES, ranges))
    if ranges or not adds:
        commands.append(f"bun install {flags}")
    return BumpStep("bun.lock", NODE_IMAGE, " && ".join(commands), list(bumps), ("package.json",), writes)


def yarn_step(folder: Path, bumps: dict[str, str], cutoff: datetime) -> BumpStep:
    """Yarn 2+ (`yarn up`, its lockfile only) with the repo's own Yarn, or Yarn 1 (`yarn add`). Each direct
    dependency is added at its exact new version, then gets its range back with the lock entry kept at that
    version (`yarn set resolution`, or the same edit to a Yarn 1 lockfile); a transitive one gets a
    `resolutions` entry. Yarn 4.10 and later hold back new releases themselves (npmMinimalAgeGate)."""
    folder = Path(folder)
    package, direct, writes = _transitive(folder, bumps, "resolutions")
    ranges = _ranges(package, bumps)
    specs = [shlex.quote(f"{name}@{target}") for name, target in bumps.items() if name in direct]
    if yarn_berry(folder):
        version = yarn_version(folder)
        if version is None:
            return _failing("yarn.lock", NODE_IMAGE, "error: can't tell which yarn wrote yarn.lock", bumps)
        commands = [COREPACK]
        if _at_least(version, 4, 10):  # in minutes; earlier Yarns refuse the setting
            commands.append(f"export YARN_NPM_MINIMAL_AGE_GATE={_age(cutoff, 60)}")
        commands.append("yarn up --mode=update-lockfile " + " ".join(specs) if specs else "yarn install --mode=update-lockfile")
        if ranges:
            commands.append(_node(_SET_RANGES, ranges))
            commands += [f"yarn set resolution {shlex.quote(f'{name}@npm:{spec}')} {shlex.quote(f'npm:{bumps[name]}')}"
                         for name, spec in ranges.items()]
    else:
        root = " -W" if package.get("workspaces") else ""  # adding at a workspace's root needs saying so
        saves = {"dependencies": "", "devDependencies": " --dev", "optionalDependencies": " --optional"}
        adds: dict[str, list[str]] = {}
        for name, target in bumps.items():
            if name in direct:
                adds.setdefault(saves[direct[name]], []).append(shlex.quote(f"{name}@{target}"))
        flags = "--ignore-scripts --non-interactive"
        commands = [f"yarn add {flags}{save}{root} " + " ".join(group) for save, group in adds.items()] or [f"yarn install {flags}"]
        if ranges:
            commands.append(_node(_SET_RANGES, ranges))
            commands.append(_node(_YARN1_PATTERNS, {f"{name}@{bumps[name]}": f"{name}@{spec}" for name, spec in ranges.items()}))
            commands.append(f"yarn install {flags} --frozen-lockfile")  # the edited lockfile has to satisfy package.json as it is
    return BumpStep("yarn.lock", NODE_IMAGE, " && ".join(commands), list(bumps), ("package.json",), writes)


STEPS = {"uv.lock": uv_step, "poetry.lock": poetry_step, "package-lock.json": npm_step, "pnpm-lock.yaml": pnpm_step,
         "yarn.lock": yarn_step, "bun.lock": bun_step}


def bump_steps(folder: Path, bumps: dict[str, dict[str, str]], cutoff: datetime) -> list[BumpStep]:
    """The steps for {lockfile: {package: version}}, resolving nothing released after `cutoff` (UTC).
    Nothing in `folder` changes: edits a step needs first are in its `writes`."""
    return [STEPS[lockfile](folder, bumps[lockfile], cutoff) for lockfile in LOCKFILES if bumps.get(lockfile)]


def added_versions(lockfile: str, before: str, after: str) -> list[Dependency]:
    """The package versions in the new lockfile that the old one didn't have."""
    parse = PARSERS[lockfile]
    old = {(dep.name.lower(), dep.version) for dep in (parse(before) if before.strip() else [])}
    return list(dict.fromkeys(dep for dep in (parse(after) if after.strip() else []) if (dep.name.lower(), dep.version) not in old))


def too_new(added: list[Dependency], cutoff: datetime, releases: Callable[[Dependency], dict[str, datetime]]) -> list[str]:
    """Each added version released after the cutoff, or whose release date the registry doesn't give: what the
    age guard turns away, whichever package manager resolved it."""
    first = {(dep.ecosystem, dep.name.lower()): dep for dep in reversed(added)}  # one registry call per package

    def dates(dep: Dependency) -> dict[str, datetime]:
        try:
            return releases(dep)
        except Exception:  # an unreachable registry proves nothing, so the version doesn't pass
            return {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(zip(first, pool.map(dates, first.values())))
    problems = []
    for dep in added:
        released = found[(dep.ecosystem, dep.name.lower())].get(dep.version)
        if released is None:
            problems.append(f"{dep.name} {dep.version} (no release date)")
        elif released > cutoff:
            problems.append(f"{dep.name} {dep.version} (released {released:%Y-%m-%d})")
    return problems
