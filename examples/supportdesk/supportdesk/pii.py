"""PII scrubbing: pull contact details out of a ticket and redact it before it's stored or sent to analytics."""

from openai import OpenAI
from pydantic import BaseModel

client = OpenAI()


class TicketPII(BaseModel):
    customer_name: str | None
    email: str | None
    phone: str | None
    order_id: str | None
    redacted_text: str


SYSTEM_PROMPT = """Extract the customer's contact details from the support ticket, then produce a redacted copy of it.

Rules:
- customer_name: the customer's own name, if they give it. Not the names of staff or anyone else.
- email: as an address, undoing obfuscation ("jane dot doe at gmail dot com" -> "jane.doe@gmail.com").
- phone: digits only, with a leading + and country code only if the customer gave a country code. "503-555-0142" -> "5035550142", "+44 7700 900123" -> "+447700900123".
- order_id: Fernhill order numbers look like FH-123456. Normalize to that format (uppercase, with the hyphen).
- redacted_text: the full ticket with every person's name replaced by [NAME], email addresses by [EMAIL], phone numbers by [PHONE], street addresses by [ADDRESS], and payment card numbers (full or partial) by [CARD]. Keep order numbers and product names. Change nothing else.
Use null for any field the ticket doesn't contain."""


def extract_pii(ticket: str) -> TicketPII:
    completion = client.chat.completions.parse(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": ticket},
        ],
        response_format=TicketPII,
    )
    return completion.choices[0].message.parsed
