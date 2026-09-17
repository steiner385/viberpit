"""Inbox consequence guidance (spec 2026-09-17 §4.3): what Accept and Reject do to
a PM triage proposal, under the owning product's dispatch policy.

Pure: no I/O, no DB imports. Shares the dispatch phrasing with the SDLC
explainer so the build-stage outlook and the inbox say the same sentence.
"""
from __future__ import annotations

from server.sdlc.explain import (
    WHEN_NEVER, WHEN_NEXT_TICK, WHEN_NOW, Consequence, Step, capitalize, consequence_to_wire,
    dispatch_sentence,
)
from server.sdlc.gates import GATE_BY_ID


def triage_guidance(*, feature_slug: str | None, score: float | None, policy: dict) -> dict:
    """``{"accept": …, "reject": …}`` (Consequence wire shape) for one proposal.
    *policy* is the product's EFFECTIVE policy (``effective_policy``), so the
    dispatch and minimum-status keys are always present."""
    dispatch = str(policy.get("dispatch") or "off")
    min_status = str(policy.get("queue_min_status") or "ready")
    home = f"under {feature_slug}" if feature_slug else "with no capability (a no-fit accept)"
    scored = f" with score {score:g}" if score is not None else ""
    accept = Consequence(
        True, None,
        (Step(f"Files it {home}{scored}; it leaves the inbox."),),
        (Step(capitalize(dispatch_sentence(dispatch, min_status)), spawns_session=dispatch == "auto"),),
        WHEN_NEXT_TICK if dispatch == "auto" else WHEN_NEVER,
        GATE_BY_ID["triage.accept"].reverse, "triage.accept")
    reject = Consequence(
        True, None,
        (Step("Marks it needs-human; it leaves the inbox and nothing is dispatched."),),
        (), WHEN_NOW, None, None)
    return {"accept": consequence_to_wire(accept), "reject": consequence_to_wire(reject)}
