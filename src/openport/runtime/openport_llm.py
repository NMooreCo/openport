"""OpenPort runtime: run OpenAI and Anthropic SDK code on NVIDIA Nemotron via Nebius Token Factory.

OpenPort copies this file into ported apps, so it depends only on the `openai` package and the standard library.

- `token_factory_client()` / `token_factory_async_client()` return ordinary `openai` clients pointed
  at Token Factory, for code written against the OpenAI SDK.
- `anthropic_client()` / `async_anthropic_client()` return clients that look like `anthropic.Anthropic`.
  Token Factory has no Anthropic-compatible API, so `messages.create` calls are translated to chat
  completions and the responses are translated back into Anthropic-shaped objects.
- `SwappedOpenAI` looks like an `openai.OpenAI` client but replaces the model in every call. OpenPort
  uses it to evaluate unmodified code on Nemotron.

The API key comes from the NEBIUS_API_KEY environment variable.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Union

from openai import AsyncOpenAI, OpenAI

TOKEN_FACTORY_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

ModelChoice = Union[str, Callable[[str], str]]  # not `str | ...`: apps on Python before 3.10 import this too

# Constructor options that only make sense for the original provider (keys, endpoints, Azure,
# Bedrock and Vertex settings). Everything else, like timeout and max_retries, is kept.
_PROVIDER_ONLY_OPTIONS = {
    "api_key", "base_url", "organization", "project", "webhook_secret", "websocket_base_url",
    "azure_endpoint", "azure_deployment", "api_version", "azure_ad_token", "azure_ad_token_provider",
    "auth_token", "aws_region", "aws_access_key", "aws_secret_key", "aws_session_token", "aws_profile",
    "region", "project_id", "access_token", "credentials",
}


def _client_options(options: dict) -> dict:
    return {key: value for key, value in options.items() if key not in _PROVIDER_ONLY_OPTIONS}


def _connection() -> dict:
    return {
        "base_url": os.getenv("NEBIUS_BASE_URL", TOKEN_FACTORY_BASE_URL),
        "api_key": os.environ["NEBIUS_API_KEY"],
    }


def token_factory_client(**options: Any) -> OpenAI:
    """An `openai.OpenAI` client for Token Factory."""
    return OpenAI(**_connection(), **_client_options(options))


def token_factory_async_client(**options: Any) -> AsyncOpenAI:
    """An `openai.AsyncOpenAI` client for Token Factory."""
    return AsyncOpenAI(**_connection(), **_client_options(options))


def anthropic_client(**options: Any) -> "AnthropicOnNemotron":
    """Stands in for `anthropic.Anthropic(...)`: same `messages.create` interface, served by Token Factory.
    Like `Anthropic()`, it's built without a key (NEBIUS_API_KEY unset): its first call needs one, not the import."""
    return AnthropicOnNemotron(client=_Deferred(lambda: token_factory_client(**options)))


def async_anthropic_client(**options: Any) -> "AsyncAnthropicOnNemotron":
    """Stands in for `anthropic.AsyncAnthropic(...)`."""
    return AsyncAnthropicOnNemotron(client=_Deferred(lambda: token_factory_async_client(**options)))


class _Deferred:
    """An `openai` client built when it's first used: `OpenAI()` isn't built without a key, and `Anthropic()` is."""

    def __init__(self, build: Callable[[], Any]):
        self._build = build
        self._client = None

    def _built(self) -> Any:
        if self._client is None:
            self._client = self._build()
        return self._client

    def __getattr__(self, name: str):
        if name.startswith("_"):  # not set yet (a copy being made): never the client's own
            raise AttributeError(name)
        return getattr(self._built(), name)

    def with_options(self, **options: Any) -> "_Deferred":
        return _Deferred(lambda: self._built().with_options(**options))


class UnsupportedFeature(Exception):
    """The original code uses something that has no Token Factory equivalent."""


# --- Usage metering ---------------------------------------------------------------------------


@dataclass
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0  # everything the model produced, reasoning included (what Token Factory bills)
    answer_tokens: int = 0  # the reply alone, without the reasoning that led to it
    seconds: float = 0.0


def answer_tokens(usage: Any, message: Any) -> int:
    """The completion tokens that make up the reply itself, not the model's reasoning.

    Uses the reasoning count when Token Factory reports one (Super does); otherwise (Nano doesn't)
    splits the completion by the length of the reasoning text and the reply.
    """
    completion = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    if reasoning is None:
        thought = len(_reasoning_text(message) or "") if message is not None else 0
        calls = getattr(message, "tool_calls", None) or []
        said = len(getattr(message, "content", None) or "") + sum(len(c.function.arguments or "") + len(c.function.name) for c in calls)
        reasoning = round(completion * thought / (thought + said)) if thought + said else 0
    return max(completion - reasoning, 0)


@dataclass
class UsageMeter:
    """Thread-safe running totals of calls, tokens and latency per model."""

    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, model: str, usage: Any, seconds: float, message: Any = None) -> None:
        with self._lock:
            totals = self.by_model.setdefault(model, ModelUsage())
            totals.calls += 1
            totals.seconds += seconds
            if usage is not None:
                totals.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                totals.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
                totals.answer_tokens += answer_tokens(usage, message)


def _merge_extra_body(kwargs: dict, extra_body: dict | None) -> dict:
    if extra_body:
        kwargs["extra_body"] = {**extra_body, **(kwargs.get("extra_body") or {})}
    return kwargs


# --- OpenAI SDK code ----------------------------------------------------------------------------


class SwappedOpenAI:
    """Drop-in for `openai.OpenAI` that runs chat completions on Token Factory.

    `model` is the Nemotron model to use, or a function from the original model name to one.
    `extra_body` is merged into every request (e.g. to turn Nemotron reasoning on or off).
    """

    def __init__(
        self,
        model: ModelChoice,
        client: OpenAI | None = None,
        meter: UsageMeter | None = None,
        extra_body: dict | None = None,
    ):
        self._client = client or token_factory_client()
        self._model = model
        self._meter = meter
        self._extra_body = extra_body
        self.chat = _Chat(self)
        self.beta = _Beta(self)

    def _target(self, original: str) -> str:
        return self._model(original) if callable(self._model) else self._model

    def _call(self, method: Callable, kwargs: dict):
        kwargs = _merge_extra_body(dict(kwargs), self._extra_body)
        kwargs["model"] = self._target(kwargs.get("model", ""))
        start = time.perf_counter()
        response = method(**kwargs)
        if self._meter is not None:
            self._meter.record(kwargs["model"], getattr(response, "usage", None), time.perf_counter() - start, _first_message(response))
        return response

    def __getattr__(self, name: str):
        # Anything not wrapped here (models, files, ...) goes straight to the Token Factory client.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._client, name)


class _Completions:
    def __init__(self, owner: SwappedOpenAI):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner._call(self._owner._client.chat.completions.create, kwargs)

    def parse(self, **kwargs):
        return self._owner._call(self._owner._client.chat.completions.parse, kwargs)


class _Chat:
    def __init__(self, owner: SwappedOpenAI):
        self.completions = _Completions(owner)


class _Beta:
    def __init__(self, owner: SwappedOpenAI):
        self.chat = _Chat(owner)


# --- Anthropic SDK code -------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class AnthropicUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class AnthropicMessage:
    id: str
    model: str
    content: list
    stop_reason: str
    usage: AnthropicUsage
    stop_sequence: str | None = None
    role: str = "assistant"
    type: str = "message"


_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "content_filter": "refusal"}


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read a field from a content block, whether it's a dict or an SDK object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(_get(block, "text", "") for block in content if _get(block, "type") == "text")


def _image_url(source: Any) -> str:
    if _get(source, "type") == "base64":
        return f"data:{_get(source, 'media_type')};base64,{_get(source, 'data')}"
    if _get(source, "type") == "url":
        return _get(source, "url")
    raise UnsupportedFeature(f"image source type {_get(source, 'type')!r}")


def _tool_result_text(block: Any) -> str:
    content = _get(block, "content", "")
    text = content if isinstance(content, str) else _text_of(content)
    return f"Error: {text}" if _get(block, "is_error") else text


def _convert_message(message: dict) -> list[dict]:
    role, content = message["role"], message["content"]
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if role == "assistant":
        text = "".join(_get(b, "text", "") for b in content if _get(b, "type") == "text")
        tool_calls = [
            {
                "id": _get(b, "id"),
                "type": "function",
                "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})},
            }
            for b in content
            if _get(b, "type") == "tool_use"
        ]
        converted: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            converted["tool_calls"] = tool_calls
        return [converted]

    # User turn: tool results become `tool` messages, which must directly follow the assistant's
    # tool calls, so they go first. Everything else stays in one user message.
    out: list[dict] = []
    parts: list[dict] = []
    for block in content:
        kind = _get(block, "type")
        if kind == "tool_result":
            out.append({"role": "tool", "tool_call_id": _get(block, "tool_use_id"), "content": _tool_result_text(block)})
        elif kind == "text":
            parts.append({"type": "text", "text": _get(block, "text")})
        elif kind == "image":
            parts.append({"type": "image_url", "image_url": {"url": _image_url(_get(block, "source"))}})
        else:
            raise UnsupportedFeature(f"content block type {kind!r}")
    if parts:
        only_text = all(p["type"] == "text" for p in parts)
        out.append({"role": "user", "content": "".join(p["text"] for p in parts) if only_text else parts})
    return out


def _convert_tool(tool: dict) -> dict:
    if "input_schema" not in tool:
        # Anthropic server tools (web search, code execution, ...) have a `type` and no schema.
        raise UnsupportedFeature(f"Anthropic server tool {tool.get('type') or tool.get('name')!r}")
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


def _convert_tool_choice(choice: dict) -> Any:
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice["name"]}}
    raise UnsupportedFeature(f"tool_choice type {kind!r}")


def anthropic_to_openai(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict],
    system: Any = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    stop_sequences: list[str] | None = None,
    thinking: dict | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
) -> dict:
    """Translate Anthropic `messages.create` arguments into a chat completions request."""
    converted: list[dict] = []
    if system:
        converted.append({"role": "system", "content": _text_of(system)})
    for message in messages:
        converted.extend(_convert_message(message))

    request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": converted}
    if tools:
        request["tools"] = [_convert_tool(t) for t in tools]
    if tool_choice:
        request["tool_choice"] = _convert_tool_choice(tool_choice)
        if tool_choice.get("disable_parallel_tool_use"):
            request["parallel_tool_calls"] = False
    if stop_sequences:
        request["stop"] = stop_sequences
    if temperature is not None:
        request["temperature"] = temperature
    if top_p is not None:
        request["top_p"] = top_p
    if thinking:
        enabled = thinking.get("type") in ("enabled", "adaptive")
        request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enabled}}
    return request


def _first_message(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    return getattr(choices[0], "message", None) if choices else None


def _reasoning_text(message: Any) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = getattr(message, key, None) or (getattr(message, "model_extra", None) or {}).get(key)
        if value:
            return value
    return None


def openai_to_anthropic(response: Any, *, model: str, include_thinking: bool) -> AnthropicMessage:
    """Translate a chat completion back into an Anthropic-shaped message."""
    choice = response.choices[0]
    message = choice.message
    content: list = []

    reasoning = _reasoning_text(message)
    if include_thinking and reasoning:
        content.append(ThinkingBlock(thinking=reasoning))
    if message.content:
        content.append(TextBlock(text=message.content))
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_unparsed_arguments": call.function.arguments}
        content.append(ToolUseBlock(id=call.id, name=call.function.name, input=arguments))
    if not any(isinstance(b, (TextBlock, ToolUseBlock)) for b in content):
        content.append(TextBlock(text=""))

    usage = getattr(response, "usage", None)
    stop_reason = _STOP_REASONS.get(choice.finish_reason, "end_turn")
    if message.tool_calls and choice.finish_reason != "length":  # some servers say "stop" with tool calls in the reply
        stop_reason = "tool_use"
    return AnthropicMessage(
        id=response.id,
        model=model,
        content=content,
        stop_reason=stop_reason,
        usage=AnthropicUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
    )


_ANTHROPIC_PARAMS = {
    "max_tokens", "messages", "system", "tools", "tool_choice", "stop_sequences", "thinking", "temperature", "top_p",
}


class AnthropicOnNemotron:
    """Drop-in for `anthropic.Anthropic` whose `messages.create` runs on Token Factory.

    `model` overrides the model named in each call (OpenPort's evals use this). Leave it out and each
    call's own `model` is used, which is what ported code does.
    `extra_body` is merged into every request (e.g. to turn Nemotron reasoning on or off).
    """

    def __init__(
        self,
        model: ModelChoice | None = None,
        client: OpenAI | None = None,
        meter: UsageMeter | None = None,
        extra_body: dict | None = None,
    ):
        self._client = client or token_factory_client()
        self._model = model
        self._meter = meter
        self._extra_body = extra_body
        self.messages = _Messages(self)

    def _target(self, model: str) -> str:
        if self._model is None:
            return model
        return self._model(model) if callable(self._model) else self._model

    @property
    def beta(self) -> "AnthropicOnNemotron":
        """`client.beta.messages.create(...)`: the same calls (the `betas` they name have no Token Factory equivalent)."""
        return self

    def with_options(self, **options: Any) -> "AnthropicOnNemotron":
        """A copy with other request options (timeout, max_retries...), as `Anthropic.with_options` gives. Its key and
        base URL stay Token Factory's."""
        return type(self)(self._model, self._client.with_options(**_client_options(options)), self._meter, self._extra_body)

    def close(self) -> None:
        """`client.close()`, as on Anthropic's client: closes the connections it holds."""
        self._client.close()


class AsyncAnthropicOnNemotron(AnthropicOnNemotron):
    """Drop-in for `anthropic.AsyncAnthropic`."""

    def __init__(
        self,
        model: ModelChoice | None = None,
        client: AsyncOpenAI | None = None,
        meter: UsageMeter | None = None,
        extra_body: dict | None = None,
    ):
        super().__init__(model, client or token_factory_async_client(), meter, extra_body)
        self.messages = _AsyncMessages(self)

    async def close(self) -> None:
        await self._client.close()


def _prepare(owner: AnthropicOnNemotron, model: str, kwargs: dict) -> tuple[dict, str, bool]:
    """Translate one Anthropic `messages.create` call. Returns (request, target model, include thinking)."""
    if kwargs.get("stream"):
        raise UnsupportedFeature("streaming Anthropic responses")
    target = owner._target(model)
    # metadata, service_tier, cache_control and the like have no Token Factory equivalent.
    known = {key: value for key, value in kwargs.items() if key in _ANTHROPIC_PARAMS}
    request = anthropic_to_openai(model=target, **known)
    # Later wins: client-wide settings, then the translated thinking switch, then the call's own extra_body.
    extra_body = {**(owner._extra_body or {}), **request.pop("extra_body", {}), **(kwargs.get("extra_body") or {})}
    if extra_body:
        request["extra_body"] = extra_body
    if kwargs.get("timeout") is not None:  # the call's own latency budget goes with it; Anthropic's headers don't
        request["timeout"] = kwargs["timeout"]
    thinking = known.get("thinking")
    return request, target, bool(thinking) and thinking.get("type") != "disabled"


class _Messages:
    def __init__(self, owner: AnthropicOnNemotron):
        self._owner = owner

    def create(self, *, model: str, **kwargs: Any) -> AnthropicMessage:
        owner = self._owner
        request, target, include_thinking = _prepare(owner, model, kwargs)
        start = time.perf_counter()
        response = owner._client.chat.completions.create(**request)
        if owner._meter is not None:
            owner._meter.record(target, getattr(response, "usage", None), time.perf_counter() - start, _first_message(response))
        return openai_to_anthropic(response, model=target, include_thinking=include_thinking)


class _AsyncMessages(_Messages):
    async def create(self, *, model: str, **kwargs: Any) -> AnthropicMessage:
        owner = self._owner
        request, target, include_thinking = _prepare(owner, model, kwargs)
        start = time.perf_counter()
        response = await owner._client.chat.completions.create(**request)
        if owner._meter is not None:
            owner._meter.record(target, getattr(response, "usage", None), time.perf_counter() - start, _first_message(response))
        return openai_to_anthropic(response, model=target, include_thinking=include_thinking)
