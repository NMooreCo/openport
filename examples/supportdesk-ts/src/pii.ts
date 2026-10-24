// PII scrubbing: pull contact details out of a ticket and redact it before it's stored or sent to analytics.
import OpenAI from "openai";

const client = new OpenAI();

export interface TicketPII {
  customer_name: string | null;
  email: string | null;
  phone: string | null;
  order_id: string | null;
  redacted_text: string;
}

const nullableString = { type: ["string", "null"] };
const TICKET_PII_SCHEMA = {
  type: "object",
  properties: {
    customer_name: nullableString,
    email: nullableString,
    phone: nullableString,
    order_id: nullableString,
    redacted_text: { type: "string" },
  },
  required: ["customer_name", "email", "phone", "order_id", "redacted_text"],
  additionalProperties: false,
};

const SYSTEM_PROMPT = `Extract the customer's contact details from the support ticket, then produce a redacted copy of it.

Rules:
- customer_name: the customer's own name, if they give it. Not the names of staff or anyone else.
- email: as an address, undoing obfuscation ("jane dot doe at gmail dot com" -> "jane.doe@gmail.com").
- phone: digits only, with a leading + and country code only if the customer gave a country code. "503-555-0142" -> "5035550142", "+44 7700 900123" -> "+447700900123".
- order_id: Fernhill order numbers look like FH-123456. Normalize to that format (uppercase, with the hyphen).
- redacted_text: the full ticket with every person's name replaced by [NAME], email addresses by [EMAIL], phone numbers by [PHONE], street addresses by [ADDRESS], and payment card numbers (full or partial) by [CARD]. Keep order numbers and product names. Change nothing else.
Use null for any field the ticket doesn't contain.`;

export async function extractPii(ticket: string): Promise<TicketPII> {
  const response = await client.chat.completions.create({
    model: "gpt-5-mini",
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: ticket },
    ],
    response_format: { type: "json_schema", json_schema: { name: "TicketPII", schema: TICKET_PII_SCHEMA, strict: true } },
  });
  return JSON.parse(response.choices[0].message.content ?? "");
}
