"""THE approval of an approval-gated escalation ("bounced at opus; fable needs
approval", ``outcomes.escalation_for``): release the owner_action hold (signal
``escalation``) and dispatch the issue now, pinned to the approved tier.

Two callers, one sequence (constitution VI): a person's tap
(``POST /api/pm/work-items/{id}/hold/approve-escalation``) and the backlog tick's
budgeted auto-approval (#2679, ``server/pm/auto_escalation.py``). The
model-approval gate still runs — ``create_session`` parks a denylisted tier as
``pending_approval`` — and the approver IS the approval it asks for, so the parked
session is resolved through ``resolve_model_approval`` (and its
``model_approval.approve`` audit). No gate bypass: a ``block`` policy still
refuses, and if the approval step itself fails the session stays parked where the
Sessions page can approve it by hand (``approval: "pending"``).

The hold is released and committed before the dispatch, so a refused dispatch
(``status`` ≠ ``spawned``) leaves the item re-queued at the product default.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from server.db.models import WorkItem

log = logging.getLogger(__name__)


class EscalationApprovalError(ValueError):
    """The approval was refused before anything changed. ``code`` / ``status`` are
    the API's error wire shape."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


async def approve_escalation(db, orch: Any, wi: WorkItem, *, model: str, actor: str,
                             identity: str, audit_type: str = "pm.hold.escalation_approved",
                             audit_detail: dict | None = None, via: str = "api",
                             describe: Callable[[WorkItem], dict] | None = None) -> dict:
    """Approve *wi*'s escalation at *model*; returns the dispatch result plus
    ``approval`` / ``model`` — and ``item``, *describe*'s rendering of the released
    row taken before the commit expires it, when a caller passes one. Raises ``EscalationApprovalError`` (nothing changed)
    for a hold that is not an approval-gated hand-off, an unknown tier or a blocked
    one, and for the release refusals ``release_via_api`` raises."""
    from server.audit import record_audit
    from server.pm.holds import DecisionHoldError, HoldPermissionError, release_via_api
    from server.pm.outcomes import escalation_for
    from server.sessions import model_policy
    from server.sessions.model_catalog import TIER_ORDER
    from server.automations.product_policy import (policy_for_repo, skip_labels,
                                                   skips_self_closing_trackers,
                                                   trusted_authors)
    from server.pm.policy_skip import skipped_by_policy
    from server.pm.queue import (is_parked, is_self_closing_tracker, is_trusted_author,
                                 needs_unavailable_environment)
    escalation = escalation_for(wi.hold_kind, wi.hold_reason)
    if escalation is None:
        raise EscalationApprovalError(
            "bad_hold", "this hold is not an approval-gated hand-off; use Retry")
    # cairnea/KinDash#16652: the hold may predate the product's ``skip_labels``.
    # ``backlog_runner_dispatch`` does not re-rank, so without this check the tap
    # dispatches an issue the queue would skip as ``bot`` the moment the hold
    # ended — the policy's "not work" is the one thing an approval must not
    # override. The backlog tick ends such holds on its own (``policy_skip``);
    # until it does, Retry (release without dispatch) is the honest tap.
    policy = await policy_for_repo(db, wi.repo or "")
    if skipped_by_policy(author=wi.author or "", author_id=wi.author_id, labels=wi.labels or (), title=wi.title or "",
                         policy=policy):
        named = sorted({str(lbl).strip().lower() for lbl in (wi.labels or ())}
                       & skip_labels(policy))
        if named:
            why = f"it carries {', '.join(named)}"
        elif is_parked(wi.labels or ()):
            why = "a person parked it"
        elif needs_unavailable_environment(wi.labels or ()):
            why = "it needs an environment no fleet host has"
        elif not is_trusted_author(wi.author or "", trusted_authors(policy),
                                  author_id=wi.author_id):
            why = "its author is not a trusted author"
        elif (skips_self_closing_trackers(policy)
              and is_self_closing_tracker(wi.labels or (), wi.title or "",
                                          author=wi.author or "")):
            why = "it is a self-closing tracker"
        else:
            why = "it is bot noise"
        raise EscalationApprovalError(
            "skipped_by_policy",
            f"{wi.repo}#{wi.number} is skipped by this product's policy ({why}); the runner "
            "does not work it, so there is nothing to escalate — Retry releases the hold")
    model = model.strip().lower()
    if model not in TIER_ORDER:
        raise EscalationApprovalError("bad_model", f"unknown ladder tier: {model}")
    if await model_policy.resolve_model_policy(db, model=model) == "block":
        raise EscalationApprovalError("model_blocked", f"{model} is blocked by policy", 409)
    ref = f"{wi.repo}#{wi.number}"
    try:
        await release_via_api(db, wi, actor=actor, identity=identity, signal="escalation",
                              via=via)
    except DecisionHoldError as e:
        raise EscalationApprovalError("bad_hold", str(e)) from e
    except HoldPermissionError as e:
        raise EscalationApprovalError("hold_not_permitted", str(e)) from e
    await record_audit(db=db, type=audit_type, identity=identity,
                       detail={"workItemId": wi.id, "model": model,
                               "requested": escalation.needs_approval,
                               "bouncedAt": escalation.bounced_at, **(audit_detail or {})})
    item = describe(wi) if describe is not None else None
    # Committed before the orchestrator call: the dispatch claims ownership in its
    # own DB session.
    await db.commit()
    result = await orch.backlog_runner_dispatch([ref], model=model)
    approval = "not_needed"
    session_id = result.get("session_id")
    if result.get("status") == "spawned" and session_id:
        try:
            await orch.resolve_model_approval(session_id, "approve", identity=identity)
            approval = "approved"
        except ValueError:
            approval = "not_needed"   # the gate allowed the tier; it is already running
        except Exception:
            log.warning("pm.approve_escalation.approval_failed item=%s session=%s model=%s",
                        wi.id, session_id, model, exc_info=True)
            approval = "pending"      # parked; the Sessions page can approve it by hand
    out = {**result, "approval": approval, "model": model}
    if describe is not None:
        out["item"] = item
    return out
