"""Receipt reading: a customer attaches a photo of a Fernhill store receipt to a refund request, and the desk reads
the order number, purchase date and total off it, to check them against the order."""

import base64
import json
from pathlib import Path

from openai import OpenAI

client = OpenAI()
INBOX = Path(__file__).parent / "inbox"  # where the support desk saves attachments

PROMPT = """Read this Fernhill Supply Co. store receipt and reply with JSON only, shaped like
{"order_id": "FH-123456", "date": "2026-10-14", "total": 84.97}

- order_id: the order number on the receipt, written as FH- and six digits.
- date: the purchase date, as YYYY-MM-DD.
- total: the amount the customer paid, after discounts and tax, as a number. Not the subtotal, and not cash handed over or change.
Use null for anything the receipt doesn't show."""


def read_receipt(receipt: str) -> dict:
    image = base64.b64encode((INBOX / receipt).read_bytes()).decode()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
            ],
        }],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)
