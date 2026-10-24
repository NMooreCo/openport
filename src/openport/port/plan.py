"""Port plans: which Nemotron model each call site moves to, or why it stays where it is."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..scan.findings import NEEDS_REVIEW, NOTES, CallSiteFinding

# Features that keep a call on its original provider unless the plan says otherwise.
BLOCKERS = {"vision", "legacy_sdk", "embeddings", "anthropic_streaming"}


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
    """Decide each call site's target. `models` maps tiers (nano, super, ultra) to Token Factory model IDs.

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
        if choice == "keep":
            decision.keep_because((assigned or {}).get("reason") or "kept by the plan")
        elif choice:
            decision.model = models.get(choice, choice)
        elif blockers:
            decision.keep_because("; ".join(NOTES[b] for b in sorted(blockers)))
        elif finding.language == "typescript" and finding.sdk == "anthropic":
            decision.keep_because("Anthropic SDK in TypeScript: the runtime doesn't cover it yet")
        if not decision.keep:  # ported anyway: carry the scanner's warnings into the port report
            decision.notes += [NOTES[f] for f in finding.features if f in NEEDS_REVIEW - BLOCKERS]
            variant = (assigned or {}).get("variant") or {}
            decision.system_addendum = variant.get("system_addendum") or ""
            decision.system_prompts = (assigned or {}).get("system_prompts") or []
            decision.max_tokens = variant.get("max_tokens")
            decision.temperature = variant.get("temperature")
        plan[finding.id] = decision
    return plan
