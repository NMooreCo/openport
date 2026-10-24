"""Read the installed version of each dependency from a repo's lockfiles.

Phase 2 (`openport upgrade`) uses these versions to ask OSV.dev which packages are vulnerable.
Lockfiles are parsed, never installed or run. Supported: Python (requirements*.txt, uv.lock,
poetry.lock) and Node (package-lock.json, pnpm-lock.yaml).
"""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    ecosystem: str  # OSV ecosystem: "PyPI" or "npm"
    source: str  # the lockfile it came from


def parse_requirements(text: str) -> list[Dependency]:
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
            deps.append(Dependency(name, version, "PyPI", "requirements.txt"))
    return deps


def parse_toml_lock(text: str, source: str) -> list[Dependency]:
    """uv.lock and poetry.lock both list packages as `[[package]]` tables with name and version."""
    data = tomllib.loads(text)
    deps: list[Dependency] = []
    for pkg in data.get("package", []):
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            deps.append(Dependency(str(name), str(version), "PyPI", source))
    return deps


def parse_package_lock(text: str) -> list[Dependency]:
    data = json.loads(text)
    deps: list[Dependency] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfile v2/v3
        for path, info in packages.items():
            if not path or not isinstance(info, dict):
                continue  # the root project has an empty-string key
            name = path.rsplit("node_modules/", 1)[-1]
            version = info.get("version")
            if name and version:
                deps.append(Dependency(name, str(version), "npm", "package-lock.json"))
        return deps

    def walk(tree: dict | None) -> None:  # lockfile v1
        for name, info in (tree or {}).items():
            if isinstance(info, dict) and info.get("version"):
                deps.append(Dependency(name, str(info["version"]), "npm", "package-lock.json"))
                walk(info.get("dependencies"))

    walk(data.get("dependencies"))
    return deps


def parse_pnpm_lock(text: str) -> list[Dependency]:
    data = yaml.safe_load(text) or {}
    deps: list[Dependency] = []
    for key in data.get("packages") or {}:
        spec = key[1:] if key.startswith("/") else key
        spec = spec.split("(", 1)[0]  # drop the peer-deps suffix: name@1.2.3(react@18)
        at = spec.rfind("@")
        if at > 0:  # name@version, including @scope/name@version
            name, version = spec[:at], spec[at + 1 :]
        elif "/" in spec:  # older pnpm: /name/1.2.3
            name, _, version = spec.rpartition("/")
        else:
            continue
        if name and version:
            deps.append(Dependency(name, version, "npm", "pnpm-lock.yaml"))
    return deps


def read_lockfiles(root: Path) -> list[Dependency]:
    """Every dependency found across the repo's root lockfiles, deduplicated by ecosystem and name."""
    root = Path(root)
    found: list[Dependency] = []
    for req in sorted(root.glob("requirements*.txt")):
        found += parse_requirements(req.read_text(encoding="utf-8", errors="replace"))
    for name, source in (("uv.lock", "uv.lock"), ("poetry.lock", "poetry.lock")):
        path = root / name
        if path.is_file():
            found += parse_toml_lock(path.read_text(encoding="utf-8", errors="replace"), source)
    if (root / "package-lock.json").is_file():
        found += parse_package_lock((root / "package-lock.json").read_text(encoding="utf-8", errors="replace"))
    if (root / "pnpm-lock.yaml").is_file():
        found += parse_pnpm_lock((root / "pnpm-lock.yaml").read_text(encoding="utf-8", errors="replace"))

    seen: set[tuple[str, str]] = set()
    unique: list[Dependency] = []
    for dep in found:
        key = (dep.ecosystem, dep.name.lower())
        if key not in seen:
            seen.add(key)
            unique.append(dep)
    return unique
