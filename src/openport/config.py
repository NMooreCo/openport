"""Settings, read from the environment and a local .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN_FACTORY_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
TIERS = ("nano", "super", "ultra")
RUNS_DIR = Path(os.getenv("OPENPORT_RUNS_DIR", "runs"))
# Where CI installs OpenPort from (the generated GitHub Action uses it).
OPENPORT_SOURCE = os.getenv("OPENPORT_SOURCE", "git+https://github.com/NMooreCo/openport")


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
