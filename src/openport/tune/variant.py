"""Variants: the knobs the tuner can turn on a call site, applied at the client so the app's code stays unchanged.

During tuning, every request the app sends goes through a `VariantClient`, which applies the
variant (extra system instructions, the reasoning switch, a higher token limit, temperature)
before the request reaches Token Factory. The port step then writes the winning variant into the
code.
"""

import copy
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from openai import LengthFinishReasonError

from ..evals.context import CURRENT_CASE


@dataclass
class Variant:
    reasoning: bool | None = None  # Nemotron's reasoning switch; None leaves the model's default
    system_addendum: str = ""  # instructions appended to the system prompt
    max_tokens: int | None = None  # a floor: raises the call's own limit, never lowers it
    temperature: float | None = None
    rationale: str = ""  # why the engineer proposed it
    sources: list[dict] = field(default_factory=list)  # NVIDIA guidance the engineer followed: {"title", "url", "quote"}

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


def system_text(request: dict) -> str | None:
    """The system prompt of a chat completions request, as plain text."""
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") in ("system", "developer"):
            content = message.get("content")
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return content
    return None


@dataclass
class VariantClient:
    """Wraps a Token Factory client and applies a variant to every chat completion.

    It also records what the app sent before the variant was applied: the first request (`sample`)
    and every distinct system prompt (`systems`). One call site can send several prompts, like a
    review helper that loads a different prompt file per pass. And it notes which cases got a reply
    cut off at the token limit (`cutoffs`), which the app's own output rarely shows.
    """

    client: Any
    variant: Variant
    held_out: frozenset[str] = frozenset()  # cases the engineer mustn't see: their prompts aren't recorded
    sample: dict | None = field(default=None)
    systems: list[str] = field(default_factory=list)
    cutoffs: dict[str, str] = field(default_factory=dict)  # case ID -> what was cut off
    # Each case's place in the dataset. Cases run at once, so the sample and the order of the prompts follow the
    # dataset, not whichever finished first: the engineer is shown the same thing run after run.
    order: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.chat = _Chat(self)
        self._lock = threading.Lock()
        self._sample_rank: tuple | None = None
        self._system_ranks: dict[str, tuple] = {}
        self._calls: dict[object, int] = {}

    def _send(self, method_name: str, request: dict):
        system = system_text(request)
        case = CURRENT_CASE.get()
        with self._lock:
            if case not in self.held_out:  # a templated prompt can carry a held-out case's input
                self._calls[case] = self._calls.get(case, 0) + 1
                rank = (self.order.get(case, len(self.order)), str(case), self._calls[case])
                if self._sample_rank is None or rank < self._sample_rank:
                    self.sample, self._sample_rank = request, rank
                if system and (system not in self._system_ranks or rank < self._system_ranks[system]):
                    self._system_ranks[system] = rank
                    self.systems[:] = sorted(self._system_ranks, key=self._system_ranks.__getitem__)
        sent = apply_variant(self.variant, request)
        try:
            response = getattr(self.client.chat.completions, method_name)(**sent)
        except LengthFinishReasonError:  # parse() refuses a cut-off reply before the app sees it
            self._note_cutoff(sent)
            raise
        choices = getattr(response, "choices", None) or []
        if choices and getattr(choices[0], "finish_reason", None) == "length":
            self._note_cutoff(sent)
        return response

    def _note_cutoff(self, request: dict) -> None:
        case = CURRENT_CASE.get()
        if case is None:
            return
        limit = request.get("max_completion_tokens") or request.get("max_tokens")
        thinking = ((request.get("extra_body") or {}).get("chat_template_kwargs") or {}).get("enable_thinking", True)  # on by default
        budget = f"{limit} tokens" if limit else "the default token limit"
        with self._lock:
            self.cutoffs[case] = f"the model's reply was cut off at the token limit ({budget}{', reasoning on' if thinking else ''})"


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
