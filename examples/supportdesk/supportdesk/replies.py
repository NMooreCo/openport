"""Reply drafting: write the first draft of a support reply for an agent to review and send."""

from anthropic import Anthropic

client = Anthropic()

BRAND_VOICE = """You write replies for Fernhill Supply Co.'s support team.

Voice: warm, direct, outdoorsy without being cute. Plain language. At most one exclamation mark per reply. No emoji.

Rules:
- Address the customer by first name if you know it, otherwise open with "Hi there,".
- Mention the order number if the customer gave one.
- Never promise a refund, replacement or exception. Say what happens next and that the team will confirm. Agents approve refunds, not you.
- If anything suggests a safety risk, the first sentence after the greeting tells the customer to stop using the product.
- Under 150 words. No headings, no bullet lists.
- End with exactly: "— The Fernhill Support Team"

Wrap the reply in <reply></reply> tags."""


def draft_reply(ticket: str, customer_name: str | None = None, order_summary: str | None = None) -> str:
    context = []
    if customer_name:
        context.append(f"Customer name: {customer_name}")
    if order_summary:
        context.append(f"Order on file: {order_summary}")
    context_block = "\n".join(context) or "No customer record found."

    message = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=600,
        system=BRAND_VOICE,
        messages=[{
            "role": "user",
            "content": f"<context>\n{context_block}\n</context>\n\n<ticket>\n{ticket}\n</ticket>\n\nDraft the reply.",
        }],
    )
    text = message.content[0].text
    return text.split("<reply>")[-1].split("</reply>")[0].strip()
