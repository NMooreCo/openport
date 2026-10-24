// Ticket triage: route each incoming ticket to a queue and set its priority.
import OpenAI from "openai";

const client = new OpenAI();

const SYSTEM_PROMPT = `You are the triage system for Fernhill Supply Co., an online outdoor gear store.
Classify the customer's ticket.

Categories:
- billing: charges, invoices, payment methods, promo codes, price adjustments
- shipping: delivery status, delays, lost or misrouted packages, address changes before delivery
- returns: returning, exchanging or refunding an item, including damaged or defective items
- product_question: sizing, specs, compatibility, care instructions, stock questions
- account: login, password, email changes, membership, deleting an account
- technical: website or app errors, checkout bugs
- other: anything else, including spam and praise

Priority:
- urgent: safety risks (injury, fire, gas leaks, gear failing in the field), legal threats, or a customer who is stranded or at risk
- normal: most tickets
- low: general questions with no time pressure, praise, spam

If a ticket raises several issues, pick the category of the one the customer most needs resolved.
Respond with JSON only: {"category": "<category>", "priority": "<priority>"}`;

export interface Triage {
  category: string;
  priority: string;
}

export async function triageTicket(ticket: string): Promise<Triage> {
  const response = await client.chat.completions.create({
    model: "gpt-5-mini",
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: ticket },
    ],
    response_format: { type: "json_object" },
  });
  return JSON.parse(response.choices[0].message.content ?? "");
}
