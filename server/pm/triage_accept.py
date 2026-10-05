"""Accepting a triage proposal — the ONE function, for humans and the machine.

``apply_accept`` moved here from ``server/api/pm.py`` (spec 2026-09-12 product
automation policy §8, D6) so the PM Triage loop's auto-accept and the inbox's
accept routes share one write path. ``auto_accept_eligible`` is the pure gate the
loop applies before calling it.

The overlay snapshot helpers (``_overlay_snapshot`` / ``_restore_overlay``, and the
``_aware`` normalisation they both need) came with it: ``apply_accept`` WRITES the
snapshot and ``undo_triage`` READS it, so the format has one owner, and leaving
either half in the router would make this module import ``server.api.pm`` — a
cycle. ``server/api/pm.py`` imports them back from here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import Select, select

from server.audit import record_audit
from server.db.models import Feature, TriageProposal, WorkItem
from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.repo_project import (OwnershipConflict, RepoOwnershipSnapshot,
    gather_repo_ownership_snapshot, require_mutation_owner)

# Who decided a classification, as stored on ``WorkItem.classified_by``, for the two
# writers of a TRIAGE decision: the inbox accept/undo routes pass DECIDED_BY_HUMAN and
# the PM Triage loop's auto-accept passes DECIDED_BY_AUTO. Sharing the spelling is what
# stops the inbox chip and the auto-accept gate reading a typo'd variant as a machine
# decision. The overlay PATCH is NOT a caller: since spec 2026-09-18 §8 it stamps the
# resolved ACTOR string (a login, ``assistant:<login>``, ``lan@local``) rather than a
# blanket "human". Other writers stamp their own provenance — "materialize",
# "auto-link", "label-route" — which are not triage decisions either.
DECIDED_BY_HUMAN = "human"
DECIDED_BY_AUTO = "auto"


class WorkItemMissing(LookupError):
    """The proposal's work item row is gone. The API maps this to 404."""


def pending_proposal_stmt(proposal_id: str) -> Select[tuple[TriageProposal]]:
    """The ONE re-read both accept paths take before deciding a proposal — the
    inbox routes' ``_load_pending_proposal`` (server/api/pm.py) and the PM Triage
    loop's ``_pm_triage_auto_accept`` seam (server/sessions/orchestrator.py). Do
    NOT hand-roll a second ``select(TriageProposal)`` at either site.

    ``with_for_update()`` is the point. Checking ``status == "pending"`` and then
    writing is a read-then-write race, and the two writers are a HUMAN and an
    unattended loop that fires every 60 s: under Postgres READ COMMITTED both can
    read "pending", the second ``UPDATE`` blocks on the row and then lands, and
    last-writer-wins. Worst case the loop's un-edited copy overwrites a human
    accept-WITH-EDITS and ``classified_by`` reads "auto" for a decision a person
    made — which then also arms ``auto_undone`` when they undo it. The lock makes
    the loser of the race re-read the row as ``accepted`` and back out (the seam
    answers ``None``; the route answers 409 ``already_resolved``).

    Dialect-safe: SQLAlchemy's SQLite dialect renders ``FOR UPDATE`` as nothing
    (SQLite serializes writes anyway), so dev/test behaviour is unchanged and
    only Postgres — where the concurrency is real — takes the lock.
    """
    return (select(TriageProposal)
            .where(TriageProposal.id == proposal_id)
            .with_for_update())


# Titles of bot output that is never a product decision (#2683): recurring
# digests and generated-index PRs. The ONE list — the loop's dismiss pass reads it.
BOT_ARTIFACT_TITLE_PATTERNS = (
    re.compile(r"\bcost\s*&\s*architecture\s+roi\s+digest\b", re.IGNORECASE),
    re.compile(r"^docs\(fix-patterns\):\s*regenerate\s+index\b", re.IGNORECASE),
)


def is_bot_artifact(title: str | None) -> bool:
    return title is not None and any(p.search(title) for p in BOT_ARTIFACT_TITLE_PATTERNS)


def auto_accept_eligible(*, tier: str, confidence_level: str | None,
                         proposed_feature_id: str | None, feature_state: str | None,
                         auto_undone: bool | None, dispatch_off_accept: bool = False) -> bool:
    """Whether the loop may accept this proposal under a product's
    ``triage_auto_accept`` *tier* (spec §8, §13).

    ``medium`` includes ``high`` (index order in ``CONFIDENCE_LEVELS``). A
    confidence outside the vocabulary — including ``None`` — reads as ``low`` and
    is never accepted. Never a no-fit (creating a capability is a human call),
    never a target capability still ``proposed`` (its doc is unmerged), never a
    proposal a human already undid once (``auto_undone``).

    *dispatch_off_accept* (#2683) waives the confidence bar only: the caller sets
    it when the product's Dispatch is off, so nothing the accept touches is ever
    dispatched. Every structural guard above still applies.
    """
    if proposed_feature_id is None or auto_undone:
        return False
    if feature_state is None or feature_state == "proposed":
        return False
    if dispatch_off_accept:
        return True
    if tier not in CONFIDENCE_LEVELS:            # "off" or garbage
        return False
    level = confidence_level if confidence_level in CONFIDENCE_LEVELS else "low"
    return CONFIDENCE_LEVELS.index(level) >= CONFIDENCE_LEVELS.index(tier)


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a DateTime(timezone=True) value to tz-aware UTC.

    SQLite has no native timezone type: it stores DateTime(timezone=True) values
    as naive strings and hands them back tzinfo-less, unlike Postgres. Every
    datetime the accept/undo path round-trips through undo_snapshot (or compares
    against an aware `now`) goes through here so the naive/aware slip the DB clock
    rule warns about can't happen. Mirrors server/pm/reconcile_inputs.py's
    `_aware` (kept separate — that one is module-private, not a shared import
    point)."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _overlay_snapshot(wi: WorkItem) -> dict:
    at = _aware(wi.classified_at)
    held_at = _aware(wi.hold_set_at)
    return {
        "featureId": wi.feature_id, "reach": wi.reach, "impact": wi.impact,
        "effort": wi.effort, "confidence": wi.confidence, "triageState": wi.triage_state,
        "classifiedBy": wi.classified_by,
        "classifiedAt": at.isoformat() if at else None,
        "holdKind": wi.hold_kind, "holdReason": wi.hold_reason, "holdBy": wi.hold_by,
        "holdSetAt": held_at.isoformat() if held_at else None,
        "holdFingerprint": wi.hold_fingerprint,
    }


def _restore_overlay(wi: WorkItem, snap: dict) -> None:
    wi.feature_id = snap.get("featureId")
    wi.reach = snap.get("reach")
    wi.impact = snap.get("impact")
    wi.effort = snap.get("effort")
    wi.confidence = snap.get("confidence")
    wi.classified_by = snap.get("classifiedBy")
    at = snap.get("classifiedAt")
    wi.classified_at = _aware(datetime.fromisoformat(at)) if at else None
    state = snap.get("triageState")
    if state == "needs_human":
        # A pre-ladder snapshot: the removed state becomes the hold it meant.
        wi.triage_state = "unclassified"
        wi.hold_kind, wi.hold_by = "owner_action", "migration"
        wi.hold_reason, wi.hold_fingerprint = "restored pre-ladder needs_human", None
        wi.hold_set_at = None
    else:
        wi.triage_state = state
        wi.hold_kind = snap.get("holdKind")
        wi.hold_reason = snap.get("holdReason")
        wi.hold_by = snap.get("holdBy")
        held = snap.get("holdSetAt")
        wi.hold_set_at = _aware(datetime.fromisoformat(held)) if held else None
        wi.hold_fingerprint = snap.get("holdFingerprint")


async def _repropose_siblings(db, accepted: TriageProposal, feature_id: str,
                              *, project_id: str, ownership: RepoOwnershipSnapshot | None) -> list[str]:
    """Other pending no_fit proposals sharing this suggestion get re-pointed at
    the newly-merged feature so the human confirms them against the real map.

    Scoped to ``project_id``: the suggestion is a bare feature slug, which is
    unique only WITHIN a project, so an unscoped sweep would re-point unrelated
    projects' proposals at a feature they have nothing to do with — and destroy
    their ``no_fit_suggestion`` (the classifier's original suggestion) while doing
    it. Returns the ids actually mutated so the caller can audit them.
    """
    candidates = (await db.execute(
        select(TriageProposal, WorkItem.repo)
        .join(WorkItem, WorkItem.id == TriageProposal.work_item_id)
        .where(
            TriageProposal.id != accepted.id,
            TriageProposal.status == "pending",
            TriageProposal.no_fit_suggestion == accepted.no_fit_suggestion)
    )).all()
    moved: list[str] = []
    for sib, repo in candidates:
        try:
            owner = await require_mutation_owner(db, repo or "", snapshot=ownership)
        except OwnershipConflict:
            continue
        if owner is None or owner.project_id != project_id:
            continue
        sib.proposed_feature_id = feature_id
        sib.no_fit_suggestion = None  # now has a home; no longer a drift item
        moved.append(sib.id)
    return moved


async def apply_accept(db, proposal: TriageProposal, *, decided_by: str,
                       audit_identity: str, edits: dict | None = None,
                       ownership: RepoOwnershipSnapshot | None = None) -> None:
    """The ONE accept path — do NOT copy it into a second helper. Copies the
    proposal onto the WorkItem overlay, applying ``edits`` over the copied values
    when provided, marks the item ``classified`` and the proposal ``accepted``.

    ``classified_by`` is *decided_by*: ``"human"`` from the inbox routes, ``"auto"``
    from the PM Triage loop's auto-accept (spec D7) — never a tailnet identity,
    which is carried only on audit events (*audit_identity*, used here for the
    nested repropose audit and by the caller for the accept audit). The proposal
    row remains as audit."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    if wi is None:
        raise WorkItemMissing(proposal.work_item_id)
    owner = await require_mutation_owner(
        db, wi.repo or "", snapshot=ownership, allow_missing=decided_by == DECIDED_BY_HUMAN)
    feature_id = (edits or {}).get("featureId", proposal.proposed_feature_id)
    if feature_id is not None:
        feature = await db.get(Feature, feature_id)
        if feature is None or owner is None or feature.project_id != owner.project_id:
            raise OwnershipConflict("mismatch", "Feature does not belong to the repository's product.")
    # The snapshot is the pre-decision overlay; ``decidedBy`` rides BESIDE it as
    # this proposal's own, immutable record of who decided it. ``undo_triage``
    # reads that key to arm ``auto_undone`` — it cannot read ``WorkItem
    # .classified_by``, which is live overlay state any overlay PATCH rewrites to
    # "human" inside the very undo window the inbox chip invites you to poke at.
    # It is added AFTER ``_overlay_snapshot`` and is not part of that helper's
    # contract: ``_restore_overlay`` reads named overlay keys only and ignores it.
    snapshot = _overlay_snapshot(wi)
    snapshot["decidedBy"] = decided_by
    proposal.undo_snapshot = snapshot
    now = datetime.now(timezone.utc)
    edits = edits or {}
    feature_id = edits.get("featureId", proposal.proposed_feature_id)
    # Drift resolution: a no_fit whose suggested name is now a real feature
    # (the merged map-diff). Link it and re-propose orphaned siblings.
    #
    # SCOPED to the work item's own project. ``Feature.slug`` is unique only per
    # project (models.py ``uq_feature_slug``), and drift suggestions are generic
    # names ("offline-mode", "sessions", "auth") that several projects hold at
    # once — an unscoped lookup silently binds the item to ANOTHER project's
    # feature row, corrupting every consumer keyed on feature→project (the GH
    # Projects reconciler, board panes, roadmap scoring). If the item's repo maps
    # to no project we cannot scope the lookup, so the branch does not fire.
    if feature_id is None and proposal.no_fit_suggestion:
        if owner is not None:
            feat = (await db.execute(select(Feature).where(
                Feature.project_id == owner.project_id,
                Feature.slug == proposal.no_fit_suggestion))).scalar_one_or_none()
            if feat is not None:
                feature_id = feat.id
                moved = await _repropose_siblings(db, proposal, feat.id,
                                                  project_id=owner.project_id, ownership=ownership)
                if moved:
                    # Spec §7: every accept mutation is audited, and this one
                    # rewrites proposals the caller never named (destroying their
                    # no_fit_suggestion irrecoverably).
                    await record_audit(
                        db=db, type="pm.triage.repropose", identity=audit_identity,
                        detail={"proposalId": proposal.id,
                                "projectId": owner.project_id,
                                "featureId": feat.id,
                                "featureSlug": proposal.no_fit_suggestion,
                                "siblingProposalIds": moved})
    wi.feature_id = feature_id
    wi.reach = edits.get("reach", proposal.reach)
    wi.impact = edits.get("impact", proposal.impact)
    wi.effort = edits.get("effort", proposal.effort)
    wi.confidence = edits.get("confidence", proposal.confidence)
    wi.triage_state = "classified"
    wi.classified_by = decided_by
    wi.classified_at = now
    proposal.status = "accepted"
    proposal.resolved_at = now
    await db.flush()


async def apply_auto_dismiss(db, proposal: TriageProposal, *, audit_identity: str) -> bool:
    """Resolve a bot-artifact proposal as ``rejected`` WITHOUT the owner hold a
    human reject sets (#2683) — the item was never the owner's to decide. The
    snapshot (stamped ``decidedBy: auto``) makes it undoable for the same window.
    Returns False when the work item is gone."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    if wi is None:
        return False
    snapshot = _overlay_snapshot(wi)
    snapshot["decidedBy"] = DECIDED_BY_AUTO
    proposal.undo_snapshot = snapshot
    proposal.status = "rejected"
    proposal.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    await record_audit(db=db, type="pm.triage.auto_dismiss", identity=audit_identity,
                       detail={"proposalId": proposal.id, "workItemId": wi.id})
    return True


async def gather_accept_ownership(proposal_ids: list[str]) -> RepoOwnershipSnapshot:
    """Gather before the caller acquires pending-proposal row locks."""
    from server.db import db_session

    async with db_session() as db:
        repos = (await db.scalars(select(WorkItem.repo).join(
            TriageProposal, TriageProposal.work_item_id == WorkItem.id
        ).where(TriageProposal.id.in_(proposal_ids)))).all()
    return await gather_repo_ownership_snapshot({repo for repo in repos if repo})
