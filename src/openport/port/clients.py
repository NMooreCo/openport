"""Decide what happens to each SDK client once the scan knows which call sites use it."""

from dataclasses import dataclass, field
from typing import Hashable

from ..scan.findings import CallSiteFinding
from .plan import SiteDecision


@dataclass
class ClientAction:
    binding: object  # language-specific binding (where the client is constructed)
    action: str  # "replace" or "split"
    ported: list[CallSiteFinding] = field(default_factory=list)


def decide_client_actions(
    sites: list[CallSiteFinding],
    plan: dict[str, SiteDecision],
    links: dict[str, list],
    key: callable,
    splittable: callable,
) -> dict[Hashable, ClientAction]:
    """Replace a client when every call it serves is ported. Split it (add a second, Token Factory
    client) when it also serves a call that stays put and `splittable(binding, ported_sites)` allows it.
    Otherwise the ported calls stay put too, and the plan says why.

    Updates `plan` in place for calls that can't be ported.
    """
    for site in sites:
        decision = plan[site.id]
        if not decision.keep and not links.get(site.id):
            decision.keep_because(f"couldn't find where its client (`{site.receiver}`) is created")

    while True:
        users: dict[Hashable, tuple[object, list[CallSiteFinding]]] = {}
        for site in sites:
            for binding in links.get(site.id) or []:
                users.setdefault(key(binding), (binding, []))[1].append(site)

        actions, changed = {}, False
        for binding_key, (binding, using) in users.items():
            ported = [s for s in using if not plan[s.id].keep]
            kept = [s for s in using if plan[s.id].keep]
            if not ported:
                continue
            if not kept:
                actions[binding_key] = ClientAction(binding, "replace", ported)
            elif splittable(binding, ported):
                actions[binding_key] = ClientAction(binding, "split", ported)
            else:
                names = ", ".join(s.id for s in kept)
                for site in ported:
                    plan[site.id].keep_because(f"shares its client with {names}, which stays on the original provider")
                changed = True
        if not changed:
            return actions
