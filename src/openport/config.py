"""Settings, read from the environment and a local .env file."""

import os
import re
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# The .env where OpenPort is run (an installed `openport` in your own repo) wins over this checkout's own.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv()

TOKEN_FACTORY_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
TIERS = ("nano", "super", "ultra")
# Rungs used where they fit, not on every call's ladder: Nemotron 3.5 Lightning (Nano's size and price) for
# `--tiers` to try, and the Nemotron that reads images, for the calls that send them.
EXTRA_TIERS = ("lightning", "vision")
RUNS_DIR = Path(os.getenv("OPENPORT_RUNS_DIR", "runs"))
# What OpenPort writes in RUNS_DIR. Run in the repo it works on (`openport port .`), these stay out of the scan and the
# copy; anything else in a runs/ folder there is the app's own.
RUNS_FOLDERS = frozenset({"ports", "repos", "captures", "upgrades", "finetunes", "dashboard"})
RUNS_FILES = re.compile(r"(?:scan|tune|plan|baseline|smoke|port|upgrade|pr|distill)-.+\.(?:json|jsonl|diff|md)")


def openports(path: Path) -> bool:
    """Whether `path` is OpenPort's own: one of its folders or reports in RUNS_DIR, or RUNS_DIR itself when that's all
    it holds."""
    path, runs = (Path(os.path.normcase(p.resolve())) for p in (path, RUNS_DIR))
    if path == runs:
        return path.is_dir() and all(openports(child) for child in path.iterdir())
    return path.parent == runs and (path.name in RUNS_FOLDERS or RUNS_FILES.fullmatch(path.name) is not None)


# Where CI installs OpenPort from (the generated GitHub Action uses it).
OPENPORT_SOURCE = os.getenv("OPENPORT_SOURCE", "git+https://github.com/NMooreCo/openport")
# The actions OpenPort's workflows use, pinned to the commits of releases, as this repo's own CI is: the jobs hold
# the repo's NEBIUS_API_KEY (and the upgrade job can push), so a moved tag mustn't change what runs.
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1"
SETUP_UV_ACTION = "astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884  # v10.3.0"


def openport_source() -> str:
    """OPENPORT_SOURCE at the commit this OpenPort came from, when that's known, so a workflow it writes runs the
    code that wrote it rather than whatever the branch holds later. An explicit @ref in OPENPORT_SOURCE is kept."""
    if not OPENPORT_SOURCE.startswith("git+") or "@" in OPENPORT_SOURCE.removeprefix("git+").split("://", 1)[-1]:
        return OPENPORT_SOURCE
    commit = _own_commit()
    return f"{OPENPORT_SOURCE}@{commit}" if commit else OPENPORT_SOURCE


def _own_commit() -> str | None:
    import importlib.metadata
    import json
    import re
    import subprocess

    try:  # installed from git: pip and uv record the commit
        direct = json.loads(importlib.metadata.distribution("openport").read_text("direct_url.json") or "{}")
        if commit := direct.get("vcs_info", {}).get("commit_id"):
            return commit
    except (importlib.metadata.PackageNotFoundError, ValueError):
        pass
    checkout = Path(__file__).resolve().parents[2]  # run from a clone of OpenPort itself
    if 'name = "openport"' not in ((checkout / "pyproject.toml").read_text(encoding="utf-8") if (checkout / "pyproject.toml").is_file() else ""):
        return None
    try:
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return head if re.fullmatch(r"[0-9a-f]{40}", head) else None


class MissingKeyError(RuntimeError):
    pass


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MissingKeyError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def pinned_model(tier: str) -> str | None:
    """A model the user pinned for this tier via OPENPORT_MODEL_<TIER>, if any."""
    return os.getenv(f"OPENPORT_MODEL_{tier.upper()}") or None
