"""Global priority queue loader (spec 003 §7a): turns the mirror's ``GhProjectItem`` rows,
across every onboarded product with a linked Projects v2 board, into ``queue.QueueRow`` — the
shape ``queue.rank`` ranks against. No ranking here; that stays in the pure core (``queue.py``,
Task 2). Nothing calls this yet (Task 4 wires it into the API)."""
from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from server.db.models import GhProjectItem, Session as DbSession, WorkItem, WorkItemDep

from .desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES
from .mirror_store import deps_in_repos, list_product_views
from .queue import QueueRow
from .reconcile_inputs import _aware

_LIVE_SESSION_STATUSES = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES


def _split_ref(ref: str) -> tuple[str, int] | None:
    repo, _, number = ref.rpartition("#")
    if repo and number.isdigit():
        return repo, int(number)
    return None


def load_queue_rows(db: Session, *, now: datetime,
                    claimed_refs: AbstractSet[str] = frozenset()) -> list[QueueRow]:
    """One ``QueueRow`` per open, board-linked issue across every onboarded product.

    A product without a linked board (``gh_project_db_id`` is ``None``) contributes
    nothing. Blockers fail closed (spec §10): an active dep edge whose destination
    issue is not mirrored has unknown state and counts as an open blocker; a
    mirrored destination counts only while it is itself open. ``now`` is accepted
    (not read here) to match every other mirror_store-style loader's signature —
    ``rank()`` is what actually needs it, for the needs-human cooldown window.
    """
    rows: list[QueueRow] = []
    # Only origin_ref is read here, so select that column directly rather than
    # loading full Session ORM rows. Deliberately NOT the same definition as
    # reconcile_inputs (latest session per ref) — this is "any live session for
    # this ref" — they agree in practice because archiving always flips status to
    # a non-live value, so a ref never has both a live and a non-live session.
    live_refs = {
        ref for ref in
        db.scalars(select(DbSession.origin_ref).where(DbSession.status.in_(_LIVE_SESSION_STATUSES)))
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
        parsed_dst = [split for split in (_split_ref(r) for r in dst_refs) if split is not None]
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
            ))
    return rows
