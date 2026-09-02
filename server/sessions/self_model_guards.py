"""Loop/nag guards for self-directed model switching.

A session that can move its own model can oscillate or nag. These rules bound that
without needing the orchestrator, so they are testable in isolation. They answer only
"should this request be considered, and may it auto-apply?" — the cheaper/costlier
decision belongs to model_policy.resolve_self_directed_model_policy.

State is per-session and in-memory: it bounds nagging, not correctness, so losing it
across a daemon restart is acceptable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SelfModelState:
    """Per-session history of self-directed model moves."""

    # How many switches this session has auto-applied (approved upgrades don't count:
    # a human already reviewed those).
    auto_applied: int = 0
    # Tiers this session has moved AWAY from — returning to one is a flip-flop.
    tiers_left: set[str] = field(default_factory=set)
    # Tiers a human answered with a different model — the session must not re-ask.
    amended_away: set[str] = field(default_factory=set)


def evaluate_guards(
    state: SelfModelState, *, current_tier: str | None, requested_tier: str | None,
    cap: int,
) -> str:
    """Return "drop" | "auto_ok" | "force_park" for a self-directed request.

    - "drop": ignore entirely (no audit-worthy event happened).
    - "force_park": may proceed, but must NOT auto-apply — a human decides.
    - "auto_ok": no guard objects; the policy decides apply vs park.
    """
    if requested_tier is not None and requested_tier == current_tier:
        return "drop"
    if requested_tier is not None and requested_tier in state.amended_away:
        return "drop"
    if requested_tier is not None and requested_tier in state.tiers_left:
        return "force_park"
    if state.auto_applied >= cap:
        return "force_park"
    return "auto_ok"
