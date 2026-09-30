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

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from server.db.models import (AutomationTargetState, GhProjectItem, Session as DbSession,
                              WorkItem, WorkItemDep)
from .desired_state import LIVE_SESSION_STATUSES
from .mirror_store import deps_in_repos, list_product_views
from .outcomes import body_fingerprint, bounces_stmt, rows_to_bounces
from .queue import QueueRow
from .reconcile_inputs import _aware
from .refs import split_ref

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
    # Only origin_ref is read here, so select that column directly rather than
    # loading full Session ORM rows. Deliberately NOT the same definition as
    # reconcile_inputs (latest session per ref) — this is "any live session for
    # this ref" — they agree in practice because archiving always flips status to
    # a non-live value, so a ref never has both a live and a non-live session.
    live_refs = {
        ref for ref in
        db.scalars(select(DbSession.origin_ref).where(DbSession.status.in_(LIVE_SESSION_STATUSES)))
        if ref
    }

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
        dst_refs = {d.dst_ref for d in deps if d.dst_ref}
        parsed_dst = [split for split in (split_ref(r) for r in dst_refs) if split is not None]
        dst_state: dict[str, str] = {}
        if parsed_dst:  # guard: an empty in_() is a SQLAlchemy warning, not a no-op query
            dst_state = {
                f"{w.repo}#{w.number}": w.state or "open"
                for w in db.scalars(select(WorkItem).where(
                    WorkItem.kind == "issue",
                    tuple_(WorkItem.repo, WorkItem.number).in_(parsed_dst)))
            }
        blockers: dict[str, list[str]] = {}
        for d in deps:
            if not d.src_ref or not d.dst_ref:
                continue
            # Unmirrored dst is absent from dst_state ⇒ unknown state ⇒ fails closed
            # (default "open", spec §10). A mirrored dst counts only while open.
            if dst_state.get(d.dst_ref, "open") == "open":
                blockers.setdefault(d.src_ref, []).append(d.dst_ref)

        product_slug = product.slug or product.project_id
        for it, w in items:
            ref = f"{w.repo}#{w.number}"
            rows.append(QueueRow(
                ref=ref, repo=w.repo or "", number=int(w.number or 0), product=product_slug,
                title=w.title or "", body=w.body or "", url=w.url or "",
                status=it.status, score=it.score,
                gh_created_at=_aware(w.gh_created_at), gh_updated_at=_aware(w.gh_updated_at),
                assignees=tuple(w.assignees or ()), author=w.author or "",
                labels=tuple(w.labels or ()),
                open_children=int(open_children.get(w.id, 0)),
                open_blockers=tuple(sorted(blockers.get(ref, ()))),
                live_session=ref in live_refs, claimed=ref in claimed_refs,
                hold_kind=w.hold_kind,
                bounces=bounces.get(ref, 0),
                hold_reason=w.hold_reason, hold_by=w.hold_by,
                hold_set_at=_aware(w.hold_set_at),
            ))
    return rows
