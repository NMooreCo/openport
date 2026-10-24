"""Prices for the closed models an app moves off, looked up live with Tavily.

Token Factory publishes its prices in /v1/models. OpenAI and Anthropic prices aren't in any API
and change often, so OpenPort searches the providers' own pricing pages with Tavily, and Nemotron
Nano reads the numbers out of the results. A price is only accepted if both numbers appear in the
text of the page it's attributed to. Results are cached in runs/prices.json, and you can set prices
by hand: `--price gpt-5-mini=0.25/2.00` (USD per million input/output tokens).
"""

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable

from . import config, tokenfactory

CACHE = config.RUNS_DIR / "prices.json"
PRICING_DOMAINS = {
    "openai": ["openai.com", "platform.openai.com"],
    "anthropic": ["anthropic.com", "docs.anthropic.com", "claude.com", "docs.claude.com"],
}

# search(query, domains) -> [{"url", "title", "content"}]; extract(model, results) -> {"input", "output", "source"}
Search = Callable[[str, list[str]], list[dict]]
Extract = Callable[[str, list[dict]], dict | None]


@dataclass
class Price:
    model: str
    input_per_m: float  # USD per million input tokens
    output_per_m: float
    source: str  # URL, "Token Factory" or "manual"
    checked: str  # ISO date

    def cost(self, prompt_tokens: float, completion_tokens: float) -> float:
        return (prompt_tokens * self.input_per_m + completion_tokens * self.output_per_m) / 1_000_000


def provider_of(model: str) -> str:
    return "anthropic" if model.lower().startswith("claude") else "openai"


def parse_manual(spec: str) -> Price:
    """"gpt-5-mini=0.25/2.00" -> Price."""
    model, _, numbers = spec.partition("=")
    input_price, _, output_price = numbers.partition("/")
    return Price(model.strip(), float(input_price), float(output_price), "manual", date.today().isoformat())


def _load_cache() -> dict[str, Price]:
    if not CACHE.exists():
        return {}
    return {model: Price(**data) for model, data in json.loads(CACHE.read_text(encoding="utf-8")).items()}


def _save_cache(prices: dict[str, Price]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({m: asdict(p) for m, p in prices.items()}, indent=2), encoding="utf-8")


def tavily_search(query: str, domains: list[str]) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=config.require("TAVILY_API_KEY"))
    response = client.search(query, search_depth="advanced", max_results=5, include_domains=domains)
    return response.get("results", [])


EXTRACT_PROMPT = """From the search results below, find the standard API price of the model `{model}`: USD per 1 million input tokens and per 1 million output tokens. Use the regular price, not batch, cached-input or long-context prices. If the exact model isn't priced in the results, use null.

Respond with JSON only: {{"input": number or null, "output": number or null, "source": "the URL the prices come from"}}

{results}"""


def nano_extract(model: str, results: list[dict]) -> dict | None:
    text = "\n\n".join(f"URL: {r.get('url')}\n{(r.get('content') or '')[:3000]}" for r in results)
    response = tokenfactory.client().chat.completions.create(
        model=tokenfactory.tiers()["nano"],
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(model=model, results=text)}],
        max_tokens=4096,
    )
    match = re.search(r"\{.*\}", response.choices[0].message.content or "", re.DOTALL)
    try:
        return json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None


def _appears(number: float, text: str) -> bool:
    """Is this price written in the text? Accepts 2.5, 2.50, $2.5 and so on."""
    forms = {f"{number:g}", f"{number:.2f}", f"{number:.3f}".rstrip("0").rstrip(".")}
    return any(re.search(rf"(?<![\d.]){re.escape(form)}(?![\d])", text) for form in forms)


def verified(extracted: dict | None, results: list[dict]) -> tuple[float, float, str] | None:
    """Accept an extracted price only if both numbers appear in the text of the cited page."""
    if not extracted:
        return None
    try:
        input_price, output_price = float(extracted["input"]), float(extracted["output"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < input_price < 1000 and 0 < output_price < 1000):
        return None
    source = extracted.get("source") or ""
    cited = [r for r in results if r.get("url") == source] or results
    for result in cited:
        text = result.get("content") or ""
        if _appears(input_price, text) and _appears(output_price, text):
            return input_price, output_price, result.get("url") or source
    return None


def closed_model_price(model: str, *, search: Search | None = None, extract: Extract | None = None, use_cache: bool = True) -> Price | None:
    cache = _load_cache() if use_cache else {}
    if model in cache:
        return cache[model]
    search = search or tavily_search
    extract = extract or nano_extract
    results = search(f"{model} API pricing per million input output tokens", PRICING_DOMAINS[provider_of(model)])
    found = verified(extract(model, results), results)
    if found is None:
        return None
    price = Price(model, found[0], found[1], found[2], date.today().isoformat())
    if use_cache:
        _save_cache(cache | {model: price})
    return price


def token_factory_price(model: str) -> Price | None:
    info = tokenfactory.model_info(model)
    if info is None or info.prompt_price is None or info.completion_price is None:
        return None
    return Price(model, info.prompt_price * 1_000_000, info.completion_price * 1_000_000, "Token Factory", date.today().isoformat())


def available() -> bool:
    return bool(os.getenv("TAVILY_API_KEY")) and bool(os.getenv("NEBIUS_API_KEY"))
