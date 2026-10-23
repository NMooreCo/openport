"""Nebius Token Factory: client, model catalog, Nemotron tier detection and pricing."""

from dataclasses import dataclass
from functools import cache

import httpx
from openai import OpenAI

from . import config

# Models whose names match these are never picked as a text tier.
_NOT_TEXT_TIERS = ("omni", "vl", "embed", "rerank", "reward", "safety", "guard", "parse")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_length: int | None
    # USD per token, as Token Factory reports them.
    prompt_price: float | None
    completion_price: float | None
    raw: dict

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        if self.prompt_price is None or self.completion_price is None:
            return None
        return prompt_tokens * self.prompt_price + completion_tokens * self.completion_price


def client() -> OpenAI:
    return OpenAI(base_url=config.TOKEN_FACTORY_BASE_URL, api_key=config.require("NEBIUS_API_KEY"))


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@cache
def catalog() -> dict[str, ModelInfo]:
    """Every model Token Factory lists, with pricing (GET /v1/models?verbose=true)."""
    response = httpx.get(
        config.TOKEN_FACTORY_BASE_URL.rstrip("/") + "/models",
        params={"verbose": "true"},
        headers={"Authorization": f"Bearer {config.require('NEBIUS_API_KEY')}"},
        timeout=30,
    )
    response.raise_for_status()
    models = {}
    for item in response.json().get("data", []):
        pricing = item.get("pricing") or {}
        models[item["id"]] = ModelInfo(
            id=item["id"],
            context_length=item.get("context_length"),
            prompt_price=_float(pricing.get("prompt")),
            completion_price=_float(pricing.get("completion")),
            raw=item,
        )
    return models


def nvidia_models() -> list[ModelInfo]:
    return [m for m in catalog().values() if m.id.lower().startswith("nvidia/") or "nemotron" in m.id.lower()]


def pick_tier(tier: str, model_ids: list[str]) -> str | None:
    """Pick the plain-text Nemotron model for a tier from a list of model IDs."""
    candidates = [
        m for m in model_ids
        if "nemotron" in m.lower() and tier in m.lower() and not any(tag in m.lower() for tag in _NOT_TEXT_TIERS)
    ]
    # Prefer Nemotron 3 over older generations, then the shortest (most canonical) name.
    candidates.sort(key=lambda m: ("nemotron-3" not in m.lower() and "nemotron3" not in m.lower(), len(m)))
    return candidates[0] if candidates else None


@cache
def tiers() -> dict[str, str]:
    """Map each tier (nano, super, ultra) to a Token Factory model ID."""
    ids = list(catalog())
    resolved = {}
    for tier in config.TIERS:
        model = config.pinned_model(tier) or pick_tier(tier, ids)
        if model is None:
            raise RuntimeError(
                f"No Nemotron {tier} model found on Token Factory. "
                f"Set OPENPORT_MODEL_{tier.upper()} in .env (see `openport models`)."
            )
        resolved[tier] = model
    return resolved


def model_info(model_id: str) -> ModelInfo | None:
    return catalog().get(model_id)


def reasoning(enabled: bool) -> dict:
    """extra_body that turns Nemotron 3's reasoning on or off (it's on by default)."""
    return {"chat_template_kwargs": {"enable_thinking": enabled}}
