"""Refund agent: looks up the order, applies the returns policy, and issues, denies or escalates the refund."""

import json

from openai import OpenAI

from .store import POLICY, RefundSession

client = OpenAI()


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


ORDER_ID = {"type": "string", "description": "Order number, e.g. FH-123456"}
REASON = {"type": "string", "description": "Short explanation, citing the policy section"}

TOOLS = [
    _tool(
        "lookup_order",
        "Fetch an order: customer, membership, delivery date, items, shipping cost and refund history.",
        {"order_id": ORDER_ID},
        ["order_id"],
    ),
    _tool(
        "issue_refund",
        "Refund the customer. Final action.",
        {"order_id": ORDER_ID, "amount": {"type": "number", "description": "Amount in USD"}, "reason": REASON},
        ["order_id", "amount", "reason"],
    ),
    _tool("deny_refund", "Deny the refund request. Final action.", {"order_id": ORDER_ID, "reason": REASON}, ["order_id", "reason"]),
    _tool(
        "escalate_to_human",
        "Hand the case to a human supervisor. Final action.",
        {"order_id": ORDER_ID, "reason": REASON},
        ["order_id", "reason"],
    ),
]

SYSTEM_PROMPT = """You are Fernhill Supply Co.'s refund agent. Today is {today}.

Look up the order first. Then apply the policy below and take exactly one final action:
issue_refund, deny_refund or escalate_to_human. Refund amounts are in USD.

{policy}"""


def handle_refund_request(ticket: str, today: str) -> dict:
    session = RefundSession()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=today, policy=POLICY)},
        {"role": "user", "content": ticket},
    ]
    for _ in range(6):
        response = client.chat.completions.create(model="gpt-5", messages=messages, tools=TOOLS)
        message = response.choices[0].message
        messages.append(message)
        if not message.tool_calls:
            break
        for call in message.tool_calls:
            result = session.run(call.function.name, json.loads(call.function.arguments))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
        if session.final:
            break
    return session.outcome()
