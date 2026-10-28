"""Global priority queue loader (spec 003 §7a): turns the mirror's ``GhProjectItem`` rows,
across every onboarded product with a linked Projects v2 board, into ``queue.QueueRow`` — the
shape ``queue.rank`` ranks against. No ranking here; that stays in the pure core (``queue.py``).

Two readers, one composition: the backlog runner's candidate pool
(``SessionOrchestrator._backlog_query_queue``) and the operator's ``GET /api/pm/queue``.
The board's "held" source
is the hold on the item itself (``WorkItem.hold_kind``, spec 2026-09-18 §3), threaded straight
onto each ``QueueRow``; the queue's own claim source is ``runner_claims``, which both queue
readers call, so what the operator sees is what agents will do next."""
from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.db.models import (AutomationTargetState, GhProjectItem, Session as DbSession,
                              WorkItem, WorkItemDep)
from .desired_state import LIVE_SESSION_STATUSES
from .drift import is_open_blocker
from .holds import OWNER_EVIDENCE, EvidenceRequest, evidence_surfaced
from .mirror_store import blocker_states, blocker_states_stmt, deps_in_repos, list_product_views
from .outcomes import (body_fingerprint, bounces_stmt, not_needed_stmt,
                       not_needed_suppression, rows_to_bounces, verdicts_by_ref)
from .queue import QueueRow
from .reconcile_inputs import _aware
from .session_ownership import (
    MAX_ANCESTOR_DEPTH, missing_parents, owned_by_live_session, owner_marker,
)

# ``BacklogRunner.id``, as a literal: importing ``server.automations.backlog_runner``
# from ``server.pm`` to read it would drag the whole automation registry into the
# queue loader. ``test_pm_queue_store`` pins the two together.
RUNNER_AUTOMATION_ID = "backlog-runner"


def runner_claims(db: Session) -> frozenset[str]:
    """Refs the backlog runner currently claims, from its durable target-state rows.

    ONE derivation, read by both queue consumers (the runner's own pool and
    ``GET /api/pm/queue``) so the operator's view cannot disagree with what agents
    will do next. A bundle rides on the primary's row; a row without ``issues`` IS
    its own claim. A ``gave_up`` row is NOT a claim — and since spec 2026-09-18 it
    is not a queue signal at all: the hold on the item is (``QueueRow.hold_kind``).
    """
    claimed: set[str] = set()
    rows = db.scalars(select(AutomationTargetState).where(
        AutomationTargetState.automation_id == RUNNER_AUTOMATION_ID))
    for row in rows:
        st = row.state or {}
        if st.get("claimed") and not st.get("gave_up"):
            claimed.update(st.get("issues") or [row.target])
    return frozenset(claimed)


def queue_inputs(s: Session, *, now: datetime) -> tuple[list[QueueRow], dict[str, dict]]:
    """Rows + per-product policies for the queue-shaped views, composed in ONE sync
    session (run it via ``db.run_sync``): which threshold a product's issues are
    judged against decides what a view shows, so a policy edit must not land between
    the two reads. THE composition ``GET /api/pm/queue|holds`` and the
    decision-escalation loop share, so no two views of the pool can disagree."""
    from server.automations.product_policy import policies_by_slug
    return load_queue_rows(s, now=now, claimed_refs=runner_claims(s)), policies_by_slug(s)


def _ownership_nodes(db: Session, items: list[WorkItem]
                     ) -> dict[str, tuple[str, str | None, str | None]]:
    """``{id: (ref, body, parent_id)}`` for *items* and their ancestors, up to
    ``MAX_ANCESTOR_DEPTH`` — the graph ``owned_by_live_session`` walks. One query per
    ancestor level, never one per item."""
    def node(w: WorkItem) -> tuple[str, str | None, str | None]:
        # A comment-borne claim (#3152) is folded into the text the marker scan reads.
        text = "\n".join([w.body or "", *map(owner_marker, w.comment_owner_ids or ())])
        return f"{w.repo}#{w.number}", text, w.parent_work_item_id

    nodes = {w.id: node(w) for w in items}
    frontier = missing_parents(nodes, nodes)
    for _ in range(MAX_ANCESTOR_DEPTH):
        if not frontier:
            break
        fetched = db.scalars(select(WorkItem).where(WorkItem.id.in_(frontier))).all()
        nodes.update((w.id, node(w)) for w in fetched)
        frontier = missing_parents(nodes, [w.id for w in fetched])
    return nodes


def load_queue_rows(db: Session, *, now: datetime,
                    claimed_refs: AbstractSet[str] = frozenset()) -> list[QueueRow]:
    """One ``QueueRow`` per open, board-linked issue across every onboarded product.

    A product without a linked board (``gh_project_db_id`` is ``None``) contributes
    nothing. Blockers fail closed (spec §10): an active dep edge whose destination
    issue is not mirrored has unknown state and counts as an open blocker; a
    mirrored destination counts only while it is itself open. ``now`` is accepted
    (not read here) to match every other mirror_store-style loader's signature.

    The item's own ``hold_kind`` (spec 2026-09-18 §3) rides straight onto the row
    — the loader only threads it, the "held" skip rule itself lives in
    ``queue.skip_reason``.
    """
    rows: list[QueueRow] = []
    # Only id + origin_ref are read here, so select those columns directly rather than
    # loading full Session ORM rows. Deliberately NOT the same definition as
    # reconcile_inputs (latest session per ref) — this is "any live session for
    # this ref" — they agree in practice because archiving always flips status to
    # a non-live value, so a ref never has both a live and a non-live session.
    live = db.execute(select(DbSession.id, DbSession.origin_ref)
                      .where(DbSession.status.in_(LIVE_SESSION_STATUSES))).all()
    live_refs = {ref for _, ref in live if ref}
    # #3076: a live session also owns the descendants of its origin ref, and any item
    # (or ancestor) whose body carries its ownership marker.
    live_ids = {sid for sid, _ in live}

    for product in list_product_views(db):
        if not product.gh_project_db_id:
            continue

        items = db.execute(
            select(GhProjectItem, WorkItem)
            .join(WorkItem, WorkItem.id == GhProjectItem.work_item_id)
            .where(GhProjectItem.gh_project_id == product.gh_project_db_id,
                   WorkItem.kind == "issue", WorkItem.state == "open")
        ).all()
        if not items:
            continue

        # D8: per-product bounce lookup — only FINALISED outcomes whose
        # ``body_fingerprint`` matches this mirror row's CURRENT body count, so an
        # edited issue (a new fingerprint) drops its stale bounce history.
        pairs = [(f"{w.repo}#{w.number}", body_fingerprint(w.title, w.body)) for _, w in items]
        # Row -> plain tuple: ``Result.all()`` is typed ``Sequence[Row[Any]]``, not
        # structurally a ``tuple[str, str, int]`` (mypy), though it unpacks as one.
        bounce_rows = [(ref, fp, n) for ref, fp, n in db.execute(bounces_stmt(pairs)).all()]
        bounces = rows_to_bounces(bounce_rows, pairs) if pairs else {}
        # #2683: every not-needed verdict on these issues, judged against each
        # issue's CURRENT material below (``outcomes.not_needed_suppression``).
        verdicts = verdicts_by_ref(
            db.execute(not_needed_stmt([ref for ref, _ in pairs])).all())

        open_children: dict[str | None, int] = {
            parent_id: count for parent_id, count in db.execute(
                select(WorkItem.parent_work_item_id, func.count(WorkItem.id))
                .where(WorkItem.parent_work_item_id.in_([w.id for _, w in items]),
                       WorkItem.state == "open")
                .group_by(WorkItem.parent_work_item_id))
        }

        deps = db.scalars(
            select(WorkItemDep).where(deps_in_repos(product.repos), WorkItemDep.active.is_(True))
        ).all()
        stmt = blocker_states_stmt({d.dst_ref for d in deps if d.dst_ref})
        dst_state = blocker_states(db.execute(stmt).all()) if stmt is not None else {}
        blockers: dict[str, list[str]] = {}
        for d in deps:
            if not d.src_ref or not d.dst_ref:
                continue
            # Unmirrored dst is absent from dst_state ⇒ unknown state ⇒ fails closed
            # (spec §10). A mirrored dst — issue or PR — counts only while open.
            if is_open_blocker(dst_state.get(d.dst_ref)):
                blockers.setdefault(d.src_ref, []).append(d.dst_ref)

        nodes = _ownership_nodes(db, [w for _, w in items])

        product_slug = product.slug or product.project_id
        merged_prs = _merged_pr_refs(db, [w for _it, w in items])
        for it, w in items:
            ref = f"{w.repo}#{w.number}"
            not_needed_until, not_needed_standing = not_needed_suppression(
                verdicts.get(ref, ()), title=w.title, body=w.body, labels=w.labels,
                last_human_comment_at=w.last_human_comment_at,
                last_reopened_at=w.last_reopened_at)
            rows.append(QueueRow(
                ref=ref, repo=w.repo or "", number=int(w.number or 0), product=product_slug,
                title=w.title or "", body=w.body or "", url=w.url or "",
                status=it.status, score=it.score,
                gh_created_at=_aware(w.gh_created_at), gh_updated_at=_aware(w.gh_updated_at),
                assignees=tuple(w.assignees or ()), author=w.author or "", author_id=w.author_id,
                labels=tuple(w.labels or ()),
                open_children=int(open_children.get(w.id, 0)),
                open_blockers=tuple(sorted(blockers.get(ref, ()))),
                live_session=owned_by_live_session(
                    w.id, nodes=nodes, live_refs=live_refs, live_ids=live_ids),
                claimed=ref in claimed_refs,
                hold_kind=w.hold_kind,
                bounces=bounces.get(ref, 0),
                hold_reason=w.hold_reason, hold_by=w.hold_by,
                hold_set_at=_aware(w.hold_set_at),
                work_item_id=w.id,
                not_needed_until=not_needed_until,
                not_needed_standing=not_needed_standing,
                last_human_comment_at=_aware(w.last_human_comment_at),
                hold_detail=w.hold_detail if w.hold_kind == OWNER_EVIDENCE else None,
                evidence_waiting=(w.hold_kind == OWNER_EVIDENCE and not evidence_surfaced(
                    EvidenceRequest.from_wire(w.hold_detail, fallback_request=w.hold_reason or ""),
                    now=now, merged_refs=merged_prs)),
            ))
    return rows


def _merged_pr_refs(db: Session, items) -> frozenset[str]:
    """The ``owner/repo#N`` PRs named as an ``owner_evidence`` precondition
    (``afterPr``, #3113) that the mirror shows merged — ONE read per product."""
    from sqlalchemy import tuple_

    from .refs import split_ref
    wanted = {str((w.hold_detail or {}).get("afterPr") or "") for w in items
              if w.hold_kind == OWNER_EVIDENCE and isinstance(w.hold_detail, dict)}
    parts = [p for p in (split_ref(r) for r in wanted if r) if p is not None]
    if not parts:
        return frozenset()
    rows = db.execute(select(WorkItem.repo, WorkItem.number).where(
        WorkItem.kind == "pr", WorkItem.state == "merged",
        tuple_(WorkItem.repo, WorkItem.number).in_(parts))).all()
    return frozenset(f"{repo}#{number}" for repo, number in rows)
