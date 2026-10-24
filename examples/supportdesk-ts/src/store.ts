// Order records, the returns policy, and the actions the refund agent can take.
import { readFileSync } from "node:fs";

const DATA = new URL("../data/", import.meta.url);
const read = (name: string) => readFileSync(new URL(name, DATA), "utf8").replace(/\r\n/g, "\n");

export const POLICY = read("policy.md");

interface Order {
  order_id: string;
  [field: string]: unknown;
}

interface Action {
  action: "refund" | "deny" | "escalate";
  order_id: string;
  amount?: number;
  reason: string;
}

export interface Outcome {
  action: string;
  amount: number | null;
}

/** Runs the refund agent's tool calls against the order book and records what it decided. */
export class RefundSession {
  orders: Record<string, Order>;
  actions: Action[] = [];

  constructor() {
    const orders: Order[] = JSON.parse(read("orders.json"));
    this.orders = Object.fromEntries(orders.map((order) => [order.order_id, order]));
  }

  get final(): Action | undefined {
    return this.actions[0];
  }

  outcome(): Outcome {
    return this.final ? { action: this.final.action, amount: this.final.amount ?? null } : { action: "none", amount: null };
  }

  run(tool: string, args: Record<string, unknown>): object {
    const orderId = String(args.order_id ?? "");
    const reason = String(args.reason ?? "");
    switch (tool) {
      case "lookup_order":
        return this.orders[orderId.trim().toUpperCase()] ?? { error: `No order ${orderId}` };
      case "issue_refund": {
        const amount = Math.round(Number(args.amount) * 100) / 100;
        if (!orderId || Number.isNaN(amount)) return { error: "Bad arguments for issue_refund: order_id and amount are required" };
        this.actions.push({ action: "refund", order_id: orderId, amount, reason });
        return { status: "refund issued", order_id: orderId, amount };
      }
      case "deny_refund":
        this.actions.push({ action: "deny", order_id: orderId, reason });
        return { status: "denial sent to customer", order_id: orderId };
      case "escalate_to_human":
        this.actions.push({ action: "escalate", order_id: orderId, reason });
        return { status: "escalated to a supervisor", order_id: orderId };
      default:
        return { error: `Unknown tool ${tool}` };
    }
  }
}
