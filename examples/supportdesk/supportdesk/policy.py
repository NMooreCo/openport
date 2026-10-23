"""Supervisor briefs: recommend a decision on an escalated refund case, for a human supervisor to sign off."""

import re

from anthropic import Anthropic

from .store import POLICY

client = Anthropic()

SYSTEM_PROMPT = f"""You prepare decision briefs for Fernhill Supply Co.'s support supervisors. Every case was escalated by an agent: it's over the agent's refund limit, flagged for repeat refunds, or asks for an exception.

Apply the policy exactly. Work through the dates, membership, item condition and refund history before deciding.

<policy>
{POLICY}
</policy>

Decisions:
- approve: refund the full amount the customer asked for
- partial: refund some, but less than the customer asked for
- deny: no refund

Respond in exactly this format:
<decision>approve|partial|deny</decision>
<refund_amount>amount in USD, 0 if denied</refund_amount>
<brief>Two or three sentences for the supervisor: the decision and the policy sections behind it.</brief>"""


def _tag(text: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def review_case(case_file: str, today: str) -> dict:
    message = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 8000},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Today is {today}.\n\n<case>\n{case_file}\n</case>"}],
    )
    text = next(block.text for block in message.content if block.type == "text")
    return {
        "decision": _tag(text, "decision").lower(),
        "refund_amount": float(_tag(text, "refund_amount").replace("$", "").replace(",", "") or 0),
        "brief": _tag(text, "brief"),
    }
