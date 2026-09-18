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


def hold_guidance(*, hold_kind: str | None, policy: dict) -> dict | None:
    """``{"retry": …, "implement": …}`` for an owner_action hold (spec 2026-09-18 §7):
    what the Retry tap and the Implement-now tap do under the owning product's
    dispatch policy. ``{"evidence": …}`` for an evidence hold (spec 2026-09-18 §5):
    one entry, no buttons — the Backlog Runner dispatches and settles it on its own,
    so there is nothing here for a person to tap. ``None`` for any other kind — a
    stronger-model hold climbs the ladder on its own (item 4a) and a decision hold
    resolves in the inbox (item 5); neither has a button here yet, so neither gets
    copy."""
    if hold_kind == "evidence":
        investigate = Consequence(
            True, None, (),
            (Step("The next run investigates this report instead of implementing "
                  "it: it closes noise, consolidates duplicates, converts it into "
                  "a fix, or hands it to you with what is needed. No pull "
                  "request."),),
            WHEN_NEXT_TICK, None, None)
        return {"evidence": consequence_to_wire(investigate)}
    if hold_kind != "owner_action":
        return None
    dispatch = str(policy.get("dispatch") or "off")
    min_status = str(policy.get("queue_min_status") or "ready")
    retry = Consequence(
        True, None,
        (Step("Releases the hold; the item is queue-eligible again at its score."),),
        (Step(capitalize(dispatch_sentence(dispatch, min_status)), spawns_session=dispatch == "auto"),),
        WHEN_NEXT_TICK if dispatch == "auto" else WHEN_NEVER,
        "Hold it again from Edit details or the overlay.", None)
    implement = Consequence(
        dispatch != "off",
        None if dispatch != "off" else
        "This product's dispatch is off; turn it on in the product's automation policy first.",
        (Step("Releases the hold and dispatches a coding session for this issue now.",
              spawns_session=True),),
        (Step("The session opens a PR; a merge closes the issue.", writes_github=True),),
        WHEN_NOW, "Archive the session; the item returns to the queue.", None)
    return {"retry": consequence_to_wire(retry), "implement": consequence_to_wire(implement)}
