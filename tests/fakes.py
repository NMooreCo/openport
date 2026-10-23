"""A scripted stand-in for the Token Factory client, so tests run without keys or network."""

import json
from types import SimpleNamespace
from typing import Callable

from openai.types.chat import ChatCompletion


def completion(content: str | None = None, tool_calls: list[tuple[str, str, dict]] | None = None, reasoning: str | None = None) -> ChatCompletion:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in tool_calls
        ]
    if reasoning:
        message["reasoning_content"] = reasoning
    return ChatCompletion.model_validate({
        "id": "cmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


class FakeTokenFactory:
    """`script(request) -> ChatCompletion` decides each reply. Every request is recorded."""

    def __init__(self, script: Callable[[dict], ChatCompletion]):
        self.requests: list[dict] = []
        self._script = script
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create, parse=self._parse))

    def _create(self, **request):
        self.requests.append(request)
        return self._script(request)

    def _parse(self, *, response_format, **request):
        response = self._create(response_format=response_format, **request)
        content = response.choices[0].message.content
        parsed = response_format.model_validate_json(content)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, parsed=parsed))], usage=response.usage)
