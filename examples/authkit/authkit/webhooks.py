"""Outbound webhooks: tell a customer's endpoint that something happened."""

import requests


def notify(url: str, event: dict, session: requests.Session | None = None) -> int:
    """POST the event as JSON and return the endpoint's status code."""
    session = session or requests.Session()
    response = session.post(url, json=event, timeout=5)
    return response.status_code
