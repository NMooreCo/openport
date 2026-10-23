import pytest

from openport.runtime.openport_llm import (
    AnthropicOnNemotron,
    TextBlock,
    ToolUseBlock,
    UnsupportedFeature,
    anthropic_to_openai,
    openai_to_anthropic,
)

from .fakes import FakeTokenFactory, completion

WEATHER = {
    "name": "get_weather",
    "description": "Weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}


def test_request_translation_covers_system_tools_and_tool_results():
    request = anthropic_to_openai(
        model="nemotron",
        max_tokens=500,
        system=[{"type": "text", "text": "Be brief."}],
        messages=[
            {"role": "user", "content": "Weather in Oslo?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Oslo"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "4C, rain"},
                    {"type": "text", "text": "Thanks. Pack a jacket?"},
                ],
            },
        ],
        tools=[WEATHER],
        tool_choice={"type": "any"},
        thinking={"type": "enabled", "budget_tokens": 1000},
        stop_sequences=["</done>"],
    )

    assert request["messages"][0] == {"role": "system", "content": "Be brief."}
    assert request["messages"][1] == {"role": "user", "content": "Weather in Oslo?"}
    assistant = request["messages"][2]
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "Oslo"}'}
    # The tool result must come straight after the assistant's tool call, before the user's text.
    assert request["messages"][3] == {"role": "tool", "tool_call_id": "tu_1", "content": "4C, rain"}
    assert request["messages"][4] == {"role": "user", "content": "Thanks. Pack a jacket?"}
    assert request["tools"][0]["function"]["parameters"] == WEATHER["input_schema"]
    assert request["tool_choice"] == "required"
    assert request["stop"] == ["</done>"]
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}


def test_assistant_turn_can_reuse_response_blocks():
    # Agent loops often send `response.content` straight back as the assistant turn.
    blocks = [TextBlock(text="Let me look."), ToolUseBlock(id="tu_9", name="get_weather", input={"city": "Lima"})]
    request = anthropic_to_openai(model="m", max_tokens=10, messages=[{"role": "assistant", "content": blocks}])
    assert request["messages"][0]["tool_calls"][0]["id"] == "tu_9"


def test_server_tools_are_flagged_not_silently_dropped():
    with pytest.raises(UnsupportedFeature):
        anthropic_to_openai(
            model="m", max_tokens=10, messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
        )


def test_response_translation():
    response = completion(content="Here you go.", tool_calls=[("call_1", "get_weather", {"city": "Oslo"})], reasoning="thinking...")
    message = openai_to_anthropic(response, model="nemotron", include_thinking=True)
    assert [block.type for block in message.content] == ["thinking", "text", "tool_use"]
    assert message.content[2].input == {"city": "Oslo"}
    assert message.stop_reason == "tool_use"
    assert message.usage.input_tokens == 100

    # Without thinking requested, reasoning is not surfaced, so `content[0].text` still works.
    plain = openai_to_anthropic(completion(content="Hi"), model="nemotron", include_thinking=False)
    assert plain.content[0].text == "Hi"
    assert plain.stop_reason == "end_turn"


def test_client_swaps_model_and_round_trips():
    fake = FakeTokenFactory(lambda request: completion(content="<reply>Hello</reply>"))
    client = AnthropicOnNemotron("nvidia/nemotron-nano", client=fake)
    message = client.messages.create(model="claude-sonnet-5-5", max_tokens=100, system="Be kind.", messages=[{"role": "user", "content": "hi"}])
    assert message.content[0].text == "<reply>Hello</reply>"
    assert fake.requests[0]["model"] == "nvidia/nemotron-nano"
