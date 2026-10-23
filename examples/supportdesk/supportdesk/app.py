"""SupportDesk API: new tickets, refund requests and escalations."""

from datetime import date

from fastapi import FastAPI
from pydantic import BaseModel

from . import pii, policy, refunds, replies, triage
from .store import load_orders

app = FastAPI(title="SupportDesk")


class Ticket(BaseModel):
    text: str


class Case(BaseModel):
    case_file: str


def _order_summary(order_id: str | None) -> str | None:
    order = load_orders().get((order_id or "").upper())
    if order is None:
        return None
    items = ", ".join(f"{item['name']} (${item['price']:.2f})" for item in order["items"])
    return f"{order['order_id']}: {items}, delivered {order['delivered_on']}"


@app.post("/tickets")
def new_ticket(ticket: Ticket) -> dict:
    routing = triage.triage_ticket(ticket.text)
    contact = pii.extract_pii(ticket.text)
    draft = replies.draft_reply(
        ticket.text, customer_name=contact.customer_name, order_summary=_order_summary(contact.order_id)
    )
    return {"routing": routing, "redacted_text": contact.redacted_text, "draft_reply": draft}


@app.post("/refund-requests")
def refund_request(ticket: Ticket) -> dict:
    return refunds.handle_refund_request(ticket.text, today=date.today().isoformat())


@app.post("/escalations")
def escalation(case: Case) -> dict:
    return policy.review_case(case.case_file, today=date.today().isoformat())
