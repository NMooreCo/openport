"""What the scanner reports for each LLM call it finds, and what each finding means for the port."""

import re
from dataclasses import dataclass, field

# Longest suffix first, so "chat.completions.create" wins over "completions.create".
CALL_SUFFIXES: list[tuple[tuple[str, ...], str, str]] = sorted(
    [
        (("chat", "completions", "create"), "openai", "chat.completions"),
        (("chat", "completions", "parse"), "openai", "chat.completions.parse"),
        (("chat", "completions", "stream"), "openai", "chat.completions.stream"),
        (("responses", "create"), "openai", "responses"),
        (("responses", "parse"), "openai", "responses.parse"),
        (("responses", "stream"), "openai", "responses.stream"),
        (("completions", "create"), "openai", "completions"),
        (("embeddings", "create"), "openai", "embeddings"),
        (("ChatCompletion", "create"), "openai", "legacy ChatCompletion"),
        (("messages", "create"), "anthropic", "messages"),
        (("messages", "parse"), "anthropic", "messages.parse"),
        (("messages", "stream"), "anthropic", "messages.stream"),
    ],
    key=lambda entry: -len(entry[0]),
)

OPENAI_CLIENTS = {"OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"}
ANTHROPIC_CLIENTS = {
    "Anthropic", "AsyncAnthropic", "AnthropicBedrock", "AsyncAnthropicBedrock", "AnthropicVertex", "AsyncAnthropicVertex",
}

# Plain HTTP calls (requests, httpx, fetch, axios...): the endpoint the URL names, as the SDK API it matches.
# Feishu's and Lark's chat API sends messages to /im/v1/messages.
HTTP_ENDPOINTS = [
    (re.compile(r"(?<!/im)/v1/messages\b|api\.anthropic\.com"), "messages"),
    (re.compile(r"/v1/responses\b"), "responses"),
    (re.compile(r"/v1/embeddings\b"), "embeddings"),
    (re.compile(r"/chat/completions\b|api\.openai\.com"), "chat.completions"),
]
# The rest of OpenAI's API: files, audio, images and so on aren't chat calls.
NOT_CHAT = re.compile(r"/v1/(?:files|audio|images|fine_tuning|fine-tunes|moderations|models|batches|uploads|vector_stores|"
                      r"assistants|threads|realtime|organization)\b")

NOT_APP_DIRS = {"test", "tests", "__tests__", "spec", "e2e", "evals", "benchmark", "benchmarks", "docs", "doc", "documentation", "versioned_docs"}

# Features that need a person to look, rather than a mechanical rewrite.
NEEDS_REVIEW = {
    "vision", "audio", "web_search", "prefill", "ends_on_assistant", "dynamic_kwargs", "legacy_sdk", "embeddings",
    "anthropic_streaming", "anthropic_parse", "raw_http", "anthropic_http",
}

NOTES = {
    "anthropic": "Anthropic SDK: translated, since Token Factory has no Anthropic-compatible API",
    "responses_api": "OpenAI Responses API: Token Factory documents /v1/responses, but OpenPort's evals test chat completions only, so check it by hand",
    "structured_output": "Structured output: needs JSON-schema support on the chosen model",
    "json_mode": "JSON mode: needs json_object support on the chosen model",
    "ends_on_assistant": "Conversation ends on an assistant turn: models handle this differently, check by hand",
    "tools": "Tool calling",
    "thinking": "Extended thinking / reasoning effort: mapped to Nemotron's reasoning switch",
    "streaming": "Streams its response",
    "anthropic_streaming": "Streams an Anthropic response: not supported by the runtime yet",
    "anthropic_parse": "Anthropic's messages.parse (structured output): not supported by OpenPort's runtime yet",
    "vision": "Sends images: needs a Nemotron that reads images, or stays on the original",
    "audio": "Sends or returns audio: Nemotron on Token Factory works in text, so it stays on the original",
    "web_search": "Searches the web as it answers: Nemotron on Token Factory can't, so it stays on the original",
    "prefill": "Assistant prefill: no chat-completions equivalent, so the prompt needs adjusting",
    "prompt_caching": "cache_control hints: no equivalent, dropped",
    "dynamic_kwargs": "Arguments built at runtime: check by hand",
    "base_url_configurable": "Client already takes a base URL",
    "legacy_sdk": "Pre-1.0 OpenAI SDK: upgrade first",
    "embeddings": "Embeddings, not a chat call: out of scope",
    "raw_http": "Plain HTTP call: its URL, key and model move to Token Factory's where the code spells them out; check for "
                "request options or response fields only the old API has",
    "anthropic_http": "Plain HTTP call to Anthropic's Messages API: Token Factory has no Anthropic-style endpoint, so it moves to "
                      "the chat-completions format first",
}


@dataclass
class CallSiteFinding:
    file: str  # relative to the repo root, with forward slashes
    line: int
    language: str  # "python" or "typescript"
    function: str  # enclosing function, e.g. "ReviewAgent.run", or "<module>"
    sdk: str  # "openai" or "anthropic", or "http" for a plain HTTP call to an LLM endpoint
    api: str  # e.g. "chat.completions", "chat.completions.parse", "responses", "messages"
    receiver: str  # the client expression, e.g. "client" or "self.client"
    receiver_kind: str  # module_global | imported | local | attribute | factory | other
    model: str | None = None  # the model name, when it's a literal or a constant
    model_expr: str | None = None  # the source expression, when the model isn't a plain literal
    output: str = "text"  # text | json_in_text | json_object | json_schema | tools | embedding
    features: list[str] = field(default_factory=list)
    in_tests: bool = False
    module: str | None = None  # dotted import path (Python only)
    id: str = ""

    @property
    def portability(self) -> str:
        """auto: swap as-is. translate: mechanical rewrite. review: needs a person."""
        if NEEDS_REVIEW & set(self.features):
            return "review"
        if self.sdk == "anthropic" or "responses_api" in self.features:
            return "translate"
        return "auto"

    @property
    def notes(self) -> list[str]:
        notes = [NOTES[f] for f in self.features if f in NOTES]
        if self.model is None and self.model_expr:
            notes.append(f"Model set by `{self.model_expr}`")
        return notes


def features_for_api(sdk: str, api: str) -> list[str]:
    features = []
    if sdk == "anthropic":
        features.append("anthropic")
    if api.startswith("responses"):
        features.append("responses_api")
    if api.endswith("parse"):
        features.append("structured_output")
        if sdk == "anthropic":  # the stand-ins for Anthropic's SDK have messages.create only
            features.append("anthropic_parse")
    if api.endswith("stream"):
        features.append("anthropic_streaming" if sdk == "anthropic" else "streaming")
    if api == "embeddings":
        features.append("embeddings")
    if api.startswith("legacy"):
        features.append("legacy_sdk")
    return features


def http_endpoint(url_text: str) -> str | None:
    """The API a plain HTTP call's URL (and the values it's built from) names, or None if it isn't an LLM call."""
    if NOT_CHAT.search(url_text):
        return None
    return next((api for pattern, api in HTTP_ENDPOINTS if pattern.search(url_text)), None)


def http_features(api: str) -> list[str]:
    return ["raw_http", *(["anthropic_http"] if api == "messages" else []), *(["embeddings"] if api == "embeddings" else [])]


def infer_output(api: str, features: list[str], structured_kind: str | None, parses_json: bool) -> str:
    if api == "embeddings":
        return "embedding"
    if "tools" in features:
        return "tools"
    if structured_kind:
        return structured_kind
    if parses_json:
        return "json_in_text"
    return "text"


def is_test_path(path: str) -> bool:
    """Tests, benchmarks, and documentation's code samples: calls the app itself doesn't make."""
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(part in NOT_APP_DIRS for part in parts[:-1])
        or name.startswith("test_")
        or name == "conftest.py"
        or name.endswith("_test.py")
        or re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", name) is not None  # button.test.tsx, api.spec.mjs, util.test.cjs
    )
