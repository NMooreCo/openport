// Refund agent: looks up the order, applies the returns policy, and issues, denies or escalates the refund.
import OpenAI from "openai";
import { type Outcome, POLICY, RefundSession } from "./store.ts";

const client = new OpenAI();

function tool(name: string, description: string, properties: Record<string, object>, required: string[]): OpenAI.Chat.ChatCompletionTool {
  return { type: "function", function: { name, description, parameters: { type: "object", properties, required } } };
}

const ORDER_ID = { type: "string", description: "Order number, e.g. FH-123456" };
const REASON = { type: "string", description: "Short explanation, citing the policy section" };

const TOOLS = [
  tool(
    "lookup_order",
    "Fetch an order: customer, membership, delivery date, items, shipping cost and refund history.",
    { order_id: ORDER_ID },
    ["order_id"],
  ),
  tool(
    "issue_refund",
    "Refund the customer. Final action.",
    { order_id: ORDER_ID, amount: { type: "number", description: "Amount in USD" }, reason: REASON },
    ["order_id", "amount", "reason"],
  ),
  tool("deny_refund", "Deny the refund request. Final action.", { order_id: ORDER_ID, reason: REASON }, ["order_id", "reason"]),
  tool("escalate_to_human", "Hand the case to a human supervisor. Final action.", { order_id: ORDER_ID, reason: REASON }, ["order_id", "reason"]),
];

export async function handleRefundRequest(ticket: string, today: string): Promise<Outcome> {
  const session = new RefundSession();
  const systemPrompt = `You are Fernhill Supply Co.'s refund agent. Today is ${today}.

Look up the order first. Then apply the policy below and take exactly one final action:
issue_refund, deny_refund or escalate_to_human. Refund amounts are in USD.

${POLICY}`;
  const messages: OpenAI.Chat.ChatCompletionMessageParam[] = [
    { role: "system", content: systemPrompt },
    { role: "user", content: ticket },
  ];
  for (let turn = 0; turn < 6; turn++) {
    const response = await client.chat.completions.create({ model: "gpt-5", messages, tools: TOOLS });
    const message = response.choices[0].message;
    messages.push(message);
    if (!message.tool_calls?.length) break;
    for (const call of message.tool_calls) {
      if (call.type !== "function") continue;
      const result = session.run(call.function.name, JSON.parse(call.function.arguments || "{}"));
      messages.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(result) });
    }
    if (session.final) break;
  }
  return session.outcome();
}
