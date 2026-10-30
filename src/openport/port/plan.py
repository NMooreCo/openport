"""Port plans: which Nemotron model each call site moves to, or why it stays where it is."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..scan.findings import NEEDS_REVIEW, NOTES, CallSiteFinding

# Features that keep a call on its original provider unless the plan says otherwise.
BLOCKERS = {"vision", "audio", "web_search", "legacy_sdk", "embeddings", "anthropic_streaming", "anthropic_parse"}
# The ones no plan can override: the ported code couldn't run (the runtime doesn't stream Anthropic replies, parse
# them, or embed, and a pre-1.0 SDK call has no client to swap).
UNPORTABLE = {"legacy_sdk", "embeddings", "anthropic_streaming", "anthropic_parse"}
# Plain HTTP calls the port moves only when they're OpenAI-style chat requests: why the others stay.
HTTP_KEPT = {
    "messages": NOTES["anthropic_http"],
    "responses": "Plain HTTP call to OpenAI's Responses API: Token Factory documents /v1/responses, but OpenPort moves chat-completions requests only",
}


@dataclass
class SiteDecision:
    site_id: str
    model: str | None  # Token Factory model ID, or None to keep the call where it is
    extra_body: dict | None = None
    # From tuning: written into the code by the port where it can be done safely.
    system_addendum: str = ""
    system_prompts: list[str] = field(default_factory=list)  # what the app sent, to find where prompts live
    max_tokens: int | None = None  # a floor
    temperature: float | None = None
    reason: str = ""  # why the call is kept, when it is
    notes: list[str] = field(default_factory=list)  # things a reviewer should check

    @property
    def keep(self) -> bool:
        return self.model is None

    def keep_because(self, reason: str) -> None:
        self.model = None
        self.reason = reason
        self.notes = []  # what to check after a port: nothing, when the call stays


def load_assignments(path: Path) -> dict[str, dict]:
    """A plan file: {"call_sites": {"<id>": {"model": "<tier or model ID>" | "keep", "extra_body": {...}}}}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("call_sites", data)


def build_plan(
    findings: list[CallSiteFinding],
    models: dict[str, str],
    default_tier: str = "super",
    assignments: dict[str, dict | str] | None = None,
) -> dict[str, SiteDecision]:
    """Decide each call site's target. `models` maps tiers (nano, super, ultra, and vision when Token Factory has
    a Nemotron that reads images) to Token Factory model IDs.

    An assignment is a tier, a model ID, "keep", or a dict with "model" and optional "extra_body".
    """
    assignments = assignments or {}
    plan = {}
    for finding in findings:
        if finding.in_tests:
            continue
        assigned = assignments.get(finding.id)
        if isinstance(assigned, str):
            assigned = {"model": assigned}
        decision = SiteDecision(finding.id, models[default_tier], extra_body=(assigned or {}).get("extra_body"))

        choice = (assigned or {}).get("model")
        blockers = BLOCKERS & set(finding.features)
        if "vision" in blockers and "vision" in models:  # a Nemotron reads images: the call moves to it, not to a text tier
            blockers.discard("vision")
            decision.model = models["vision"]
            if not choice:
                decision.notes.append(f"Sends images: moved to {models['vision']}, the Nemotron that reads them; check it on your own images")
        if finding.sdk == "http" and finding.api in HTTP_KEPT:
            decision.keep_because(HTTP_KEPT[finding.api])
        elif choice == "keep":
            decision.keep_because((assigned or {}).get("reason") or "kept by the plan")
        elif choice and UNPORTABLE & blockers:
            decision.keep_because("; ".join(NOTES[b] for b in sorted(UNPORTABLE & blockers)))
        elif choice:
            decision.model = models.get(choice, choice)
        elif blockers:
            decision.keep_because("; ".join(NOTES[b] for b in sorted(blockers)))
        if not decision.keep:  # ported anyway: carry the scanner's warnings into the port report
            decision.notes += [NOTES[f] for f in finding.features if f in NEEDS_REVIEW - BLOCKERS]
            variant = (assigned or {}).get("variant") or {}
            decision.system_addendum = variant.get("system_addendum") or ""
            decision.max_tokens = variant.get("max_tokens")
            decision.temperature = variant.get("temperature")
        decision.system_prompts = (assigned or {}).get("system_prompts") or []  # a kept call's too: its prompts stay as they are
        plan[finding.id] = decision
    return plan
