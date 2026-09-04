"""Facts loader for one product's reconcile cycle (spec 003 §6 steps 1–2 inputs)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import (GhProject, GhProjectItem, GhWriteJournal, Initiative,
                              Session as DbSession, WorkItem, WorkItemDep)

from .desired_state import ItemFacts
from .mirror_store import ProductSyncView, deps_in_repos
from .queue import NEEDS_HUMAN_LABEL
from .reconcile_diff import DepView, MirrorItem


@dataclass(frozen=True)
class ProductFacts:
    project_id: str
    slug: str
    owner: str
    gh_project_db_id: str | None
    project_node_id: str | None
    field_cache: dict
    mirror_synced_at: datetime | None
    items: list[ItemFacts]
    mirror: dict[str, MirrorItem]
    open_issue_nodes: dict[str, str]
    deps: list[DepView]
    node_ids: dict[str, str]
    newest_decision_at: datetime | None
    last_applied_at: datetime | None


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a DateTime(timezone=True) value to tz-aware UTC.

    SQLite has no native timezone type: it stores DateTime(timezone=True) values
    as naive strings and hands them back tzinfo-less, unlike Postgres. Every
    datetime this loader reads off a model row goes through here so ProductFacts
    is comparable/orderable regardless of dialect.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _max(*ts: datetime | None) -> datetime | None:
    present = [t for t in ts if t is not None]
    return max(present) if present else None


def load_product_facts(db: Session, product: ProductSyncView, *, now: datetime) -> ProductFacts:
    repos = set(product.repos)
    gh = db.get(GhProject, product.gh_project_db_id) if product.gh_project_db_id else None
    issues = db.scalars(select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "issue")).all()
    prs = db.scalars(select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "pr",
                                            WorkItem.state == "merged")).all()
    by_ref = {_ref(w): w for w in issues}
    by_id = {w.id: w for w in issues}
    merged_at: dict[str, datetime | None] = {}
    for pr in prs:
        for n in pr.linked_issue_numbers or []:
            ref = f"{pr.repo}#{n}"
            merged_at[ref] = _max(merged_at.get(ref),
                                  _aware(pr.gh_closed_at) or _aware(pr.gh_updated_at))
    sessions: dict[str, DbSession] = {}
    for s in db.scalars(select(DbSession).where(DbSession.origin_ref.in_(list(by_ref)))).all():
        cur = sessions.get(s.origin_ref or "")
        if cur is None or (_aware(s.updated_at) or now) > (_aware(cur.updated_at) or now):
            sessions[s.origin_ref or ""] = s
    deps = list(db.scalars(select(WorkItemDep).where(deps_in_repos(repos))).all())
    blockers: dict[str, list[str]] = {}
    blockers_at: dict[str, datetime | None] = {}
    for d in deps:
        if not d.active or not d.src_ref or not d.dst_ref:
            continue
        dst = by_ref.get(d.dst_ref)
        if dst is not None and (dst.state or "open") == "open":
            blockers.setdefault(d.src_ref, []).append(d.dst_ref)
            blockers_at[d.src_ref] = _max(blockers_at.get(d.src_ref), _aware(d.updated_at))
    initiatives = {f"{i.epic_repo}#{i.epic_number}": i for i in db.scalars(
        select(Initiative).where(Initiative.epic_repo.in_(repos))).all()}
    mirror: dict[str, MirrorItem] = {}
    if gh is not None:
        for it in db.scalars(select(GhProjectItem).where(GhProjectItem.gh_project_id == gh.id)).all():
            w = by_id.get(it.work_item_id or "")
            if w is not None and it.gh_item_node_id:
                mirror[_ref(w)] = MirrorItem(_ref(w), it.gh_item_node_id, it.status, it.score,
                                             it.stage, _aware(it.gh_updated_at))
    items: list[ItemFacts] = []
    for ref, w in by_ref.items():
        if (w.state or "open") != "open" and ref not in mirror:
            continue
        sess = sessions.get(ref)
        ini = initiatives.get(ref)
        items.append(ItemFacts(
            ref=ref, issue_state=w.state, issue_closed_at=_aware(w.gh_closed_at),
            triage_state=w.triage_state, classified_at=_aware(w.classified_at),
            reach=w.reach, impact=w.impact, effort=w.effort, confidence=w.confidence,
            session_status=sess.status if sess else None,
            session_at=_aware(sess.updated_at) if sess else None,
            merged_pr_at=merged_at.get(ref),
            open_blocker_refs=tuple(sorted(blockers.get(ref, []))), blockers_at=blockers_at.get(ref),
            needs_human_label=NEEDS_HUMAN_LABEL in (w.labels or []),
            initiative_stage=ini.stage if ini else None,
            initiative_stage_at=_aware(ini.updated_at) if ini else None))
    last_applied = _aware(db.scalar(select(GhWriteJournal.updated_at).where(
        GhWriteJournal.applied.is_(True)).order_by(GhWriteJournal.updated_at.desc()).limit(1)))
    # The freshness gate refuses to write against a mirror older than this. It must
    # therefore cover every timestamp that can move a DECISION: triage (classified_at),
    # initiative stage, our own last write — and dep edges, whose updated_at is what flips
    # an item to/from Blocked and was previously ignored here. Every dep row counts, not
    # just the ones currently blocking: DEACTIVATING the last blocker is equally a decision
    # (it unblocks the item), and `plan_dep_rows` only writes a row when it really changed,
    # so this timestamp does not churn.
    #
    # Session timestamps are deliberately NOT folded in, even though session status is a
    # decision input. `Session.updated_at` is a generic row-write time (see the model:
    # "bumped on EVERY update"), so a single live session would hold newest_decision_at
    # at ~now forever, the mirror could never catch up, and the product would log
    # skipped_stale on every tick without ever converging. Session-driven pushes stay
    # safe by another route: they carry that same near-now timestamp as their decided_at,
    # so the ownership rule lets them through against any older gh_updated_at.
    newest = _max(*(_aware(w.classified_at) for w in issues),
                  *(_aware(i.updated_at) for i in initiatives.values()),
                  *(_aware(d.updated_at) for d in deps),
                  last_applied)
    return ProductFacts(
        project_id=product.project_id, slug=product.slug or product.project_id,
        owner=(product.repos[0].split("/", 1)[0] if product.repos else ""),
        gh_project_db_id=gh.id if gh else None, project_node_id=gh.gh_node_id if gh else None,
        field_cache=dict(gh.field_cache or {}) if gh else {},
        mirror_synced_at=_aware(gh.last_synced_at) if gh else None,
        items=items, mirror=mirror,
        open_issue_nodes={ref: w.gh_node_id for ref, w in by_ref.items()
                          if (w.state or "open") == "open" and w.gh_node_id},
        deps=[DepView(d.src_ref or "", d.dst_ref or "", d.source or "", bool(d.active)) for d in deps],
        node_ids={ref: w.gh_node_id for ref, w in by_ref.items() if w.gh_node_id},
        newest_decision_at=newest, last_applied_at=last_applied)
