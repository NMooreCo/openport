"""Smoke tests: what each Nemotron tier on Token Factory actually supports.

Each check returns (ok, detail). Results feed the tuning loop's defaults and FEEDBACK.md.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Literal

from openai import OpenAI
from pydantic import BaseModel

from . import tokenfactory
from .runtime.openport_llm import _reasoning_text


@dataclass
class Check:
    tier: str
    model: str
    name: str
    ok: bool
    seconds: float
    detail: str


class TicketContact(BaseModel):
    name: str
    email: str
    priority: Literal["low", "normal", "urgent"]


ORDER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_order_status",
        "description": "Look up the shipping status of a Fernhill order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order number, e.g. FH-123456"}},
            "required": ["order_id"],
        },
    },
}

ORDER_QUESTION = [{"role": "user", "content": "Where is my order FH-100233?"}]

# Generous, because reasoning tokens can count against max_tokens.
MAX_TOKENS = 4096


def _describe(message) -> str:
    notes = []
    if _reasoning_text(message):
        notes.append("reasoning returned separately")
    if message.content and "<think>" in message.content:
        notes.append("<think> inside content")
    return "; ".join(notes)


def _checks(client: OpenAI, model: str) -> dict[str, Callable[[], tuple[bool, str]]]:
    def chat(extra_body=None):
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
            max_tokens=MAX_TOKENS,
            extra_body=extra_body,
        )
        message = response.choices[0].message
        content = (message.content or "").strip()
        detail = f"{content[:60]!r} | {response.usage.completion_tokens} output tokens"
        notes = _describe(message)
        return "pong" in content.lower(), f"{detail} | {notes}" if notes else detail

    def json_object():
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": 'Respond with JSON only, shaped {"city": str, "country": str}.'},
                {"role": "user", "content": "What is the capital of Japan?"},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
        )
        content = response.choices[0].message.content or ""
        data = json.loads(content)
        return {"city", "country"} <= data.keys(), content[:80]

    def json_schema():
        completion = client.chat.completions.parse(
            model=model,
            messages=[{
                "role": "user",
                "content": "Ticket from Jane Doe (jane.doe@example.com): my camp stove is leaking gas. "
                "Extract her name, email and the ticket priority.",
            }],
            response_format=TicketContact,
            max_tokens=MAX_TOKENS,
        )
        parsed = completion.choices[0].message.parsed
        return parsed is not None and parsed.priority == "urgent", repr(parsed)[:100]

    def tool_call(tool_choice):
        response = client.chat.completions.create(
            model=model, messages=ORDER_QUESTION, tools=[ORDER_TOOL], tool_choice=tool_choice, max_tokens=MAX_TOKENS
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            return False, f"no tool call; said {(response.choices[0].message.content or '')[:60]!r}"
        args = json.loads(calls[0].function.arguments)
        return (
            calls[0].function.name == "get_order_status" and args.get("order_id") == "FH-100233",
            f"{calls[0].function.name}({calls[0].function.arguments})",
        )

    def tool_round_trip():
        first = client.chat.completions.create(
            model=model, messages=ORDER_QUESTION, tools=[ORDER_TOOL], max_tokens=MAX_TOKENS
        )
        message = first.choices[0].message
        if not message.tool_calls:
            return False, "no tool call on the first turn"
        call = message.tool_calls[0]
        followup = ORDER_QUESTION + [
            message,  # the assistant message object, passed back the way OpenAI's docs do it
            {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"status": "out for delivery", "eta": "Tuesday"})},
        ]
        second = client.chat.completions.create(model=model, messages=followup, tools=[ORDER_TOOL], max_tokens=MAX_TOKENS)
        content = second.choices[0].message.content or ""
        return "tuesday" in content.lower(), content[:80]

    return {
        "chat": chat,
        "reasoning off": lambda: chat(tokenfactory.reasoning(False)),
        "json_object": json_object,
        "json_schema": json_schema,
        "tools (auto)": lambda: tool_call("auto"),
        "tools (forced)": lambda: tool_call({"type": "function", "function": {"name": "get_order_status"}}),
        "tool round-trip": tool_round_trip,
    }


def _run(tier: str, model: str, name: str, fn: Callable[[], tuple[bool, str]]) -> Check:
    start = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as error:  # a failed check is a result, not a crash
        ok, detail = False, f"{type(error).__name__}: {error}"[:300]
    return Check(tier, model, name, ok, round(time.perf_counter() - start, 2), detail)


def run_model_checks(concurrency: int = 12) -> list[Check]:
    client = tokenfactory.client()
    jobs = []
    for tier, model in tokenfactory.tiers().items():
        for name, fn in _checks(client, model).items():
            jobs.append((tier, model, name, fn))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda job: _run(*job), jobs))


def run_tavily_check() -> Check | None:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None

    def search():
        from tavily import TavilyClient

        result = TavilyClient(api_key=key).search("NVIDIA Nemotron 3 pricing Nebius Token Factory", max_results=3)
        hits = result.get("results", [])
        return bool(hits), hits[0]["url"] if hits else "no results"

    return _run("-", "tavily", "search", search)
