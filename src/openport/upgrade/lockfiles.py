"""Read the installed version of each dependency from a repo's lockfiles.

Phase 2 (`openport upgrade`) uses these versions to ask OSV.dev which packages are vulnerable.
Lockfiles are parsed, never installed or run. Supported: Python (requirements*.txt, uv.lock,
poetry.lock) and Node (package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock), at the repo's root
and in its subprojects (a monorepo's api/uv.lock and web/pnpm-lock.yaml).
"""

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

# Folders whose lockfiles aren't the app's: installs, builds, and test fixtures.
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "site-packages",
             ".tox", "tests", "test", "__tests__", "fixtures", "__fixtures__", "testdata"}


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    ecosystem: str  # OSV ecosystem: "PyPI" or "npm"
    source: str  # the lockfile it came from: its name at the root, else its path ("api/uv.lock")
    also: tuple[str, ...] = ()  # other lockfiles that lock this same version (a repo with package-lock.json and bun.lock)


def parse_requirements(text: str, source: str = "requirements.txt") -> list[Dependency]:
    deps: list[Dependency] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line:
            continue  # comments, options (-r, -e, --hash), and URL/VCS installs
        line = line.split(";", 1)[0].strip()  # drop environment markers
        if "==" not in line:
            continue  # only exact pins give the installed version
        name, _, version = line.partition("==")
        name = name.split("[", 1)[0].strip()  # drop extras: name[extra]
        version = version.split()[0].strip() if version.split() else ""
        if name and version:
            deps.append(Dependency(name, version, "PyPI", source))  # its own file: requirements-dev.txt pins its own
    return deps


def _from_registry(pkg: dict) -> bool:
    """Whether a locked package came from a package index. The project itself (uv's `editable = "."`), a path or
    git dependency isn't a release on PyPI, so another package's advisories under the same name aren't its."""
    origin = pkg.get("source")
    if not isinstance(origin, dict):
        return True  # poetry.lock's PyPI packages have no source
    if "type" in origin:  # poetry.lock: [package.source] type = "legacy" is a package index
        return origin["type"] == "legacy"
    return "registry" in origin  # uv.lock: registry, or editable, virtual, directory, path, git, url


def parse_toml_lock(text: str, source: str) -> list[Dependency]:
    """uv.lock and poetry.lock both list packages as `[[package]]` tables with name and version."""
    data = tomllib.loads(text)
    deps: list[Dependency] = []
    for pkg in data.get("package", []):
        name, version = pkg.get("name"), pkg.get("version")
        if name and version and _from_registry(pkg):
            deps.append(Dependency(str(name), str(version), "PyPI", source))
    return deps


def parse_package_lock(text: str) -> list[Dependency]:
    data = json.loads(text)
    deps: list[Dependency] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfile v2/v3
        for path, info in packages.items():
            # The root project has an empty-string key; a workspace's own folder (packages/ui) isn't from the registry,
            # nor is its link in node_modules.
            if "node_modules/" not in path or not isinstance(info, dict) or info.get("link"):
                continue
            # An alias (node_modules/string-width-cjs) names the package it really is.
            name = str(info.get("name") or path.rsplit("node_modules/", 1)[-1])
            version = info.get("version")
            if name and version:
                deps.append(Dependency(name, str(version), "npm", "package-lock.json"))
        return deps

    def walk(tree: dict | None) -> None:  # lockfile v1
        for name, info in (tree or {}).items():
            if isinstance(info, dict) and info.get("version"):
                version = str(info["version"])
                if version.startswith("npm:"):  # an alias: "string-width-cjs": {"version": "npm:string-width@4.2.3"}
                    name, version = _split_spec(version.removeprefix("npm:"))
                if name and version:
                    deps.append(Dependency(name, version, "npm", "package-lock.json"))
                walk(info.get("dependencies"))

    walk(data.get("dependencies"))
    return deps


# pnpm's lockfile v5 keys: /name/1.2.3 or /@scope/name/1.2.3, then any peers after "_" (/react-dom/18.2.0_react@18.2.0).
_PNPM_V5_KEY = re.compile(r"((?:@[^/]+/)?[^/@()]+)/(\d[^/_()]*)(?:_.*)?")


def _pnpm_itself(document: dict) -> bool:
    """pnpm 11 and later write a first document for pnpm's own executable: its importers hold nothing else."""
    importers = document.get("importers") or {}
    return bool(importers) and all(
        isinstance(importer, dict) and importer.get("packageManagerDependencies")
        and set(importer) <= {"configDependencies", "packageManagerDependencies"} for importer in importers.values())


def parse_pnpm_lock(text: str) -> list[Dependency]:
    deps: list[Dependency] = []
    for data in yaml.safe_load_all(text):  # one document, or (pnpm 11+) pnpm's own and then the project's
        if isinstance(data, dict) and not _pnpm_itself(data):
            deps += _pnpm_packages(data)
    return deps


def _pnpm_packages(data: dict) -> list[Dependency]:
    deps: list[Dependency] = []
    for key in data.get("packages") or {}:
        spec = key[1:] if key.startswith("/") else key
        old = _PNPM_V5_KEY.fullmatch(spec)
        spec = spec.split("(", 1)[0]  # drop the peer-deps suffix: name@1.2.3(react@18)
        at = spec.rfind("@")
        if old:  # older pnpm: /name/1.2.3, whose peers' "@" would otherwise be taken for the version's
            name, version = old[1], old[2]
        elif at > 0:  # name@version, including @scope/name@version
            name, version = spec[:at], spec[at + 1 :]
        elif "/" in spec:
            name, _, version = spec.rpartition("/")
        else:
            continue
        # A tarball URL, a folder (file:, link:) or a git repo isn't a registry release: no version of it to check
        # (lobehub's xlsx@https://cdn.sheetjs.com/... would match every xlsx advisory).
        if name and version and ":" not in name + version:
            deps.append(Dependency(name, version, "npm", "pnpm-lock.yaml"))
    return deps


def _split_spec(spec: str) -> tuple[str, str]:
    """"@scope/name@1.2.3" -> ("@scope/name", "1.2.3"); the last "@" that isn't the first character splits them."""
    at = spec.rfind("@")
    return (spec[:at], spec[at + 1 :]) if at > 0 else (spec, "")


def parse_yarn_lock(text: str) -> list[Dependency]:
    """yarn.lock, classic (v1, its own format) or Berry (v2+, YAML)."""
    deps: list[Dependency] = []
    if "__metadata:" in text:  # Berry: "name@npm:^1.0.0": {version, resolution: "name@npm:1.0.3"}
        for key, entry in (yaml.safe_load(text) or {}).items():
            if key == "__metadata" or not isinstance(entry, dict) or not entry.get("version"):
                continue
            resolution = str(entry.get("resolution") or key.split(",")[0].strip())
            name = _split_spec(resolution.split("@npm:")[0] if "@npm:" in resolution else resolution)[0]
            if name and "@workspace:" not in resolution and "@patch:" not in resolution:
                deps.append(Dependency(name, str(entry["version"]), "npm", "yarn.lock"))
        return deps
    name = None
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            first = line.rstrip()[:-1].split(",")[0].strip().strip('"')
            if "@npm:" in first:  # an alias: string-width-cjs@npm:string-width@^4.2.0 is string-width
                first = first.split("@npm:", 1)[1]
            name = _split_spec(first)[0] or None
        elif name and (match := re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)):
            deps.append(Dependency(name, match[1], "npm", "yarn.lock"))
            name = None
    return deps


def parse_bun_lock(text: str) -> list[Dependency]:
    """bun.lock (Bun 1.2+): JSON with trailing commas; each package is ["name@version", ...]."""
    data = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
    deps: list[Dependency] = []
    for entry in (data.get("packages") or {}).values():
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            name, version = _split_spec(entry[0])
            if name and re.match(r"^\d", version):  # a registry version, not a workspace or git reference
                deps.append(Dependency(name, version, "npm", "bun.lock"))
    return deps


PARSERS = {
    "uv.lock": lambda text: parse_toml_lock(text, "uv.lock"),
    "poetry.lock": lambda text: parse_toml_lock(text, "poetry.lock"),
    "package-lock.json": parse_package_lock,
    "pnpm-lock.yaml": parse_pnpm_lock,
    "yarn.lock": parse_yarn_lock,
    "bun.lock": parse_bun_lock,
}


def _lockfiles(root: Path):
    """The repo's lockfiles, root first, then its subprojects', skipping installs, builds and test fixtures."""
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name in PARSERS or re.fullmatch(r"requirements[^/]*\.txt", name):
                yield Path(directory) / name


NODE_LOCKFILES = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "bun": "bun.lock"}


def node_lockfile_order(root: Path) -> list[str]:
    """The root's Node lockfiles, the one its package manager writes first: the manager package.json names
    (packageManager), else npm's, pnpm's, Yarn's, bun's."""
    package = Path(root) / "package.json"
    try:
        named = str(json.loads(package.read_text(encoding="utf-8-sig")).get("packageManager") or "") if package.is_file() else ""
    except (ValueError, AttributeError):
        named = ""
    first = NODE_LOCKFILES.get(named.split("@")[0])
    return list(dict.fromkeys([*([first] if first else []), *NODE_LOCKFILES.values()]))


def read_lockfiles(root: Path, unread: list[str] | None = None) -> list[Dependency]:
    """Every dependency found across the repo's lockfiles, once per ecosystem, name and version. A root
    lockfile's dependencies have its name as their source; a subproject's, its path ("api/uv.lock"). When two
    lockfiles lock the same version (package-lock.json and bun.lock, say), the source is the one the project's
    package manager uses, and `also` lists the others."""
    root = Path(root)
    found: list[Dependency] = []
    for path in _lockfiles(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace").removeprefix("﻿")
        try:
            if path.name.startswith("requirements"):
                deps = parse_requirements(text, path.name)
            else:
                deps = PARSERS[path.name](text)
        except (ValueError, TypeError, AttributeError, yaml.YAMLError, tomllib.TOMLDecodeError) as error:
            if unread is not None:  # the others still count, and the caller says this one wasn't checked
                unread.append(f"{relative}: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}".rstrip(": "))
            continue
        if "/" in relative:  # a subproject's: say where
            deps = [Dependency(d.name, d.version, d.ecosystem, relative) for d in deps]
        found += deps

    order = node_lockfile_order(root)
    rank = {name: position for position, name in enumerate(order)}
    found.sort(key=lambda dep: ("/" in dep.source, rank.get(dep.source, len(order))))  # stable: the walk's order otherwise
    sources: dict[tuple[str, str, str], list[Dependency]] = {}
    for dep in found:
        sources.setdefault((dep.ecosystem, dep.name.lower(), dep.version), []).append(dep)
    return [Dependency(first.name, first.version, first.ecosystem, first.source, tuple(dict.fromkeys(d.source for d in rest if d.source != first.source)))
            for first, *rest in sources.values()]
