"""Variants: the knobs the tuner can turn on a call site, applied at the client so the app's code stays unchanged.

During tuning, every request the app sends goes through a `VariantClient`, which applies the
variant (extra system instructions, the reasoning switch, a higher token limit, temperature)
before the request reaches Token Factory. The port step then writes the winning variant into the
code.
"""

import copy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Variant:
    reasoning: bool | None = None  # Nemotron's reasoning switch; None leaves the model's default
    system_addendum: str = ""  # instructions appended to the system prompt
    max_tokens: int | None = None  # a floor: raises the call's own limit, never lowers it
    temperature: float | None = None
    rationale: str = ""  # why the engineer proposed it

    def is_plain(self) -> bool:
        return self.reasoning is None and not self.system_addendum and self.max_tokens is None and self.temperature is None

    def describe(self) -> str:
        parts = []
        if self.reasoning is not None:
            parts.append(f"reasoning {'on' if self.reasoning else 'off'}")
        if self.system_addendum:
            parts.append(f"+{len(self.system_addendum.split())} words of instructions")
        if self.max_tokens is not None:
            parts.append(f"max_tokens >= {self.max_tokens}")
        if self.temperature is not None:
            parts.append(f"temperature {self.temperature}")
        return ", ".join(parts) or "as written"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Variant":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def apply_variant(variant: Variant, request: dict) -> dict:
    """Return a copy of a chat completions request with the variant applied."""
    request = copy.deepcopy(request)
    if variant.system_addendum:
        messages = list(request.get("messages") or [])
        first = messages[0] if messages else None
        if isinstance(first, dict) and first.get("role") in ("system", "developer"):
            content = first.get("content")
            if isinstance(content, list):
                content = content + [{"type": "text", "text": variant.system_addendum}]
            else:
                content = f"{content or ''}\n\n{variant.system_addendum}".strip()
            messages[0] = {**first, "content": content}
        else:
            messages.insert(0, {"role": "system", "content": variant.system_addendum})
        request["messages"] = messages
    if variant.reasoning is not None:
        extra = dict(request.get("extra_body") or {})
        extra["chat_template_kwargs"] = {**extra.get("chat_template_kwargs", {}), "enable_thinking": variant.reasoning}
        request["extra_body"] = extra
    if variant.max_tokens is not None:
        key = "max_completion_tokens" if "max_completion_tokens" in request else "max_tokens"
        request[key] = max(request.get(key) or 0, variant.max_tokens)
    if variant.temperature is not None:
        request["temperature"] = variant.temperature
    return request


@dataclass
class VariantClient:
    """Wraps a Token Factory client; applies a variant to every chat completion and keeps a sample request."""

    client: Any
    variant: Variant
    sample: dict | None = field(default=None)

    def __post_init__(self):
        self.chat = _Chat(self)

    def _send(self, method_name: str, request: dict):
        request = apply_variant(self.variant, request)
        if self.sample is None:
            self.sample = request
        return getattr(self.client.chat.completions, method_name)(**request)


class _Completions:
    def __init__(self, owner: VariantClient):
        self._owner = owner

    def create(self, **request):
        return self._owner._send("create", request)

    def parse(self, **request):
        return self._owner._send("parse", request)


class _Chat:
    def __init__(self, owner: VariantClient):
        self.completions = _Completions(owner)
