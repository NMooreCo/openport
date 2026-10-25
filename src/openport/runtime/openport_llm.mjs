// OpenPort runtime: runs Anthropic SDK code on NVIDIA Nemotron via Nebius Token Factory.
//
// OpenPort copied this file into the app. Token Factory has no Anthropic-compatible API, so
// `messages.create` calls are translated to chat completions and the replies back into Anthropic-shaped
// messages, as the Python runtime (openport_llm.py) does. It needs nothing but Node's own fetch.
//
// The key comes from NEBIUS_API_KEY; NEBIUS_BASE_URL overrides the URL (OpenPort's evals use it).

export const TOKEN_FACTORY_BASE_URL = "https://api.tokenfactory.nebius.com/v1/";
const STOP_REASONS = { stop: "end_turn", length: "max_tokens", tool_calls: "tool_use", content_filter: "refusal" };
// Anthropic options with no Token Factory equivalent are dropped; any other key is a Token Factory option
// the port added (like chat_template_kwargs, Nemotron's reasoning switch) and goes along as it is.
const ANTHROPIC_ONLY = new Set(["metadata", "service_tier", "top_k", "container", "mcp_servers", "betas"]);
const TRANSLATED = new Set(["model", "max_tokens", "messages", "system", "tools", "tool_choice", "stop_sequences", "thinking", "temperature", "top_p", "stream"]);

export class UnsupportedFeature extends Error {}

function textOf(content) {
  return typeof content === "string" ? content : content.filter((b) => b.type === "text").map((b) => b.text ?? "").join("");
}

function imageUrl(source) {
  if (source?.type === "base64") return `data:${source.media_type};base64,${source.data}`;
  if (source?.type === "url") return source.url;
  throw new UnsupportedFeature(`image source type ${JSON.stringify(source?.type)}`);
}

function toolResultText(block) {
  const content = block.content ?? "";
  const text = typeof content === "string" ? content : textOf(content);
  return block.is_error ? `Error: ${text}` : text;
}

function convertMessage(message) {
  const { role, content } = message;
  if (typeof content === "string") return [{ role, content }];
  if (role === "assistant") {
    const text = content.filter((b) => b.type === "text").map((b) => b.text ?? "").join("");
    const toolCalls = content.filter((b) => b.type === "tool_use").map((b) => ({
      id: b.id, type: "function", function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
    }));
    const converted = { role: "assistant", content: text || null };
    if (toolCalls.length) converted.tool_calls = toolCalls;
    return [converted];
  }
  // A user turn: tool results become `tool` messages, which must directly follow the assistant's tool calls,
  // so they go first. Everything else stays in one user message.
  const out = [];
  const parts = [];
  for (const block of content) {
    if (block.type === "tool_result") out.push({ role: "tool", tool_call_id: block.tool_use_id, content: toolResultText(block) });
    else if (block.type === "text") parts.push({ type: "text", text: block.text });
    else if (block.type === "image") parts.push({ type: "image_url", image_url: { url: imageUrl(block.source) } });
    else throw new UnsupportedFeature(`content block type ${JSON.stringify(block.type)}`);
  }
  if (parts.length) {
    const onlyText = parts.every((p) => p.type === "text");
    out.push({ role: "user", content: onlyText ? parts.map((p) => p.text).join("") : parts });
  }
  return out;
}

function convertTool(tool) {
  if (!("input_schema" in tool)) throw new UnsupportedFeature(`Anthropic server tool ${JSON.stringify(tool.type ?? tool.name)}`);
  return { type: "function", function: { name: tool.name, description: tool.description ?? "", parameters: tool.input_schema } };
}

function convertToolChoice(choice) {
  if (choice.type === "auto") return "auto";
  if (choice.type === "any") return "required";
  if (choice.type === "none") return "none";
  if (choice.type === "tool") return { type: "function", function: { name: choice.name } };
  throw new UnsupportedFeature(`tool_choice type ${JSON.stringify(choice.type)}`);
}

/** Anthropic `messages.create` arguments, as a chat completions request body. */
export function anthropicToOpenAI(params) {
  const messages = [];
  if (params.system) messages.push({ role: "system", content: textOf(params.system) });
  for (const message of params.messages) messages.push(...convertMessage(message));
  const request = { model: params.model, max_tokens: params.max_tokens, messages };
  if (params.tools?.length) request.tools = params.tools.map(convertTool);
  if (params.tool_choice) {
    request.tool_choice = convertToolChoice(params.tool_choice);
    if (params.tool_choice.disable_parallel_tool_use) request.parallel_tool_calls = false;
  }
  if (params.stop_sequences?.length) request.stop = params.stop_sequences;
  if (params.temperature != null) request.temperature = params.temperature;
  if (params.top_p != null) request.top_p = params.top_p;
  if (params.thinking) request.chat_template_kwargs = { enable_thinking: ["enabled", "adaptive"].includes(params.thinking.type) };
  for (const [key, value] of Object.entries(params)) {
    if (!TRANSLATED.has(key) && !ANTHROPIC_ONLY.has(key)) request[key] = value;
  }
  return request;
}

/** A chat completion, as an Anthropic message. */
export function openAIToAnthropic(response, model, includeThinking) {
  const choice = response.choices[0];
  const message = choice.message;
  const content = [];
  const reasoning = message.reasoning_content || message.reasoning;
  if (includeThinking && reasoning) content.push({ type: "thinking", thinking: reasoning, signature: "" });
  if (message.content) content.push({ type: "text", text: message.content });
  for (const call of message.tool_calls ?? []) {
    let input;
    try {
      input = JSON.parse(call.function.arguments || "{}");
    } catch {
      input = { _unparsed_arguments: call.function.arguments };
    }
    content.push({ type: "tool_use", id: call.id, name: call.function.name, input });
  }
  if (!content.some((b) => b.type === "text" || b.type === "tool_use")) content.push({ type: "text", text: "" });
  return {
    id: response.id, type: "message", role: "assistant", model, content,
    stop_reason: STOP_REASONS[choice.finish_reason] ?? "end_turn", stop_sequence: null,
    usage: { input_tokens: response.usage?.prompt_tokens ?? 0, output_tokens: response.usage?.completion_tokens ?? 0 },
  };
}

/** Stands in for `new Anthropic(...)`: the same `messages.create`, served by Token Factory. */
export class AnthropicOnNemotron {
  constructor(options = {}) {
    this.baseURL = options.baseURL ?? process.env.NEBIUS_BASE_URL ?? TOKEN_FACTORY_BASE_URL;
    this.apiKey = options.apiKey ?? process.env.NEBIUS_API_KEY ?? "";
    this.timeout = options.timeout;
    this.messages = { create: (params) => this.#create(params) };
  }

  async #create(params) {
    if (params.stream) throw new UnsupportedFeature("streaming Anthropic responses");
    const response = await fetch(`${this.baseURL.replace(/\/$/, "")}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify(anthropicToOpenAI(params)),
      signal: this.timeout ? AbortSignal.timeout(this.timeout) : undefined,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body?.error?.message ?? body?.detail ?? JSON.stringify(body);
      throw new Error(`Token Factory returned ${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    }
    const thinking = params.thinking;
    return openAIToAnthropic(body, params.model, Boolean(thinking) && thinking.type !== "disabled");
  }
}
