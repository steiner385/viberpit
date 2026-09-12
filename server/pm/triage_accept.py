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

from datetime import datetime, timezone

from sqlalchemy import Select, select

from server.audit import record_audit
from server.db.models import Feature, TriageProposal, WorkItem
from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.repo_project import owning_projects_by_repo

# Who decided a classification, as stored on ``WorkItem.classified_by``. The one
# spelling of "a person decided this", shared by the accept routes and the overlay
# PATCH so the inbox chip and the auto-accept gate cannot read a typo'd variant as
# a machine decision. (Other writers stamp their own provenance — "materialize",
# "auto-link", "label-route" — which are not triage decisions.)
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


def auto_accept_eligible(*, tier: str, confidence_level: str | None,
                         proposed_feature_id: str | None, feature_state: str | None,
                         auto_undone: bool | None) -> bool:
    """Whether the loop may accept this proposal under a product's
    ``triage_auto_accept`` *tier* (spec §8, §13).

    ``medium`` includes ``high`` (index order in ``CONFIDENCE_LEVELS``). A
    confidence outside the vocabulary — including ``None`` — reads as ``low`` and
    is never accepted. Never a no-fit (creating a capability is a human call),
    never a target capability still ``proposed`` (its doc is unmerged), never a
    proposal a human already undid once (``auto_undone``).
    """
    if tier not in CONFIDENCE_LEVELS:            # "off" or garbage
        return False
    if proposed_feature_id is None or auto_undone:
        return False
    if feature_state is None or feature_state == "proposed":
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
    return {
        "featureId": wi.feature_id, "reach": wi.reach, "impact": wi.impact,
        "effort": wi.effort, "confidence": wi.confidence, "triageState": wi.triage_state,
        "classifiedBy": wi.classified_by,
        "classifiedAt": at.isoformat() if at else None,
    }


def _restore_overlay(wi: WorkItem, snap: dict) -> None:
    wi.feature_id = snap.get("featureId")
    wi.reach = snap.get("reach")
    wi.impact = snap.get("impact")
    wi.effort = snap.get("effort")
    wi.confidence = snap.get("confidence")
    wi.triage_state = snap.get("triageState")
    wi.classified_by = snap.get("classifiedBy")
    at = snap.get("classifiedAt")
    wi.classified_at = _aware(datetime.fromisoformat(at)) if at else None


async def _repropose_siblings(db, accepted: TriageProposal, feature_id: str,
                              *, project_id: str) -> list[str]:
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
    owners = await owning_projects_by_repo(
        db, {repo for _sib, repo in candidates if repo})
    moved: list[str] = []
    for sib, repo in candidates:
        owner = owners.get(repo) if repo else None
        if owner is None or owner.project_id != project_id:
            continue
        sib.proposed_feature_id = feature_id
        sib.no_fit_suggestion = None  # now has a home; no longer a drift item
        moved.append(sib.id)
    return moved


async def apply_accept(db, proposal: TriageProposal, *, decided_by: str,
                       audit_identity: str, edits: dict | None = None) -> None:
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
        owner = (await owning_projects_by_repo(
            db, {wi.repo} if wi.repo else set())).get(wi.repo or "")
        if owner is not None:
            feat = (await db.execute(select(Feature).where(
                Feature.project_id == owner.project_id,
                Feature.slug == proposal.no_fit_suggestion))).scalar_one_or_none()
            if feat is not None:
                feature_id = feat.id
                moved = await _repropose_siblings(db, proposal, feat.id,
                                                  project_id=owner.project_id)
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
