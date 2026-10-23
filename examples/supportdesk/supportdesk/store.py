"""Order records, the returns policy, and the actions the refund agent can take."""

import json
from pathlib import Path

DATA = Path(__file__).parent / "data"
POLICY = (DATA / "policy.md").read_text(encoding="utf-8")


def load_orders() -> dict[str, dict]:
    orders = json.loads((DATA / "orders.json").read_text(encoding="utf-8"))
    return {order["order_id"]: order for order in orders}


class RefundSession:
    """Runs the refund agent's tool calls against the order book and records what it decided."""

    def __init__(self):
        self.orders = load_orders()
        self.actions: list[dict] = []

    @property
    def final(self) -> dict | None:
        return self.actions[0] if self.actions else None

    def outcome(self) -> dict:
        if self.final is None:
            return {"action": "none", "amount": None}
        return {"action": self.final["action"], "amount": self.final.get("amount")}

    def run(self, tool: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{tool}", None)
        if handler is None:
            return {"error": f"Unknown tool {tool}"}
        try:
            return handler(**args)
        except TypeError as error:
            return {"error": f"Bad arguments for {tool}: {error}"}

    def tool_lookup_order(self, order_id: str) -> dict:
        return self.orders.get(order_id.strip().upper()) or {"error": f"No order {order_id}"}

    def tool_issue_refund(self, order_id: str, amount: float, reason: str = "") -> dict:
        self.actions.append({"action": "refund", "order_id": order_id, "amount": round(float(amount), 2), "reason": reason})
        return {"status": "refund issued", "order_id": order_id, "amount": round(float(amount), 2)}

    def tool_deny_refund(self, order_id: str, reason: str = "") -> dict:
        self.actions.append({"action": "deny", "order_id": order_id, "reason": reason})
        return {"status": "denial sent to customer", "order_id": order_id}

    def tool_escalate_to_human(self, order_id: str, reason: str = "") -> dict:
        self.actions.append({"action": "escalate", "order_id": order_id, "reason": reason})
        return {"status": "escalated to a supervisor", "order_id": order_id}
