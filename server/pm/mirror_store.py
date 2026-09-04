"""Session-level persistence for the 003 read-only mirror. Callers own the transaction."""
from __future__ import annotations

from collections.abc import Sequence, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from server.db.models import (
    INITIATIVE_STAGES, GhProject, GhProjectItem, Initiative, Project, ProjectRepo,
    WorkItem, WorkItemDep)

from .dep_plan import DepRowView, observed_deps, plan_dep_rows
from .gh_projects import GhProjectItemView, GhProjectView, IssueRelView
from .journal import own_write_key


@dataclass(frozen=True)
class ProductSyncView:
    project_id: str
    slug: str | None
    name: str | None
    gh_project_db_id: str | None
    gh_node_id: str | None
    repos: list[str]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_product_views(db: Session) -> list[ProductSyncView]:
    views = []
    for project in db.query(Project).all():
        gh = db.query(GhProject).filter_by(project_id=project.id).first()
        repos = [r.repo for r in db.query(ProjectRepo)
                 .filter_by(project_id=project.id).all() if r.repo]
        views.append(ProductSyncView(
            project_id=project.id, slug=project.slug, name=project.name,
            gh_project_db_id=gh.id if gh else None,
            gh_node_id=gh.gh_node_id if gh else None, repos=repos))
    return views


def upsert_gh_project(db: Session, project_id: str, view: GhProjectView) -> str:
    row = db.query(GhProject).filter_by(gh_node_id=view.node_id).first()
    if row is None:
        row = GhProject(gh_node_id=view.node_id)
        db.add(row)
    row.project_id = project_id
    row.gh_project_number = view.number
    row.title = view.title
    db.flush()
    return row.id


def save_field_cache(db: Session, gh_project_db_id: str, cache: dict) -> None:
    row = db.get(GhProject, gh_project_db_id)
    if row is not None:
        row.field_cache = cache


def _work_item_id(db: Session, repo: str | None, number: int | None,
                  kind: str | None) -> str | None:
    if not repo or number is None or not kind:
        return None
    item = db.query(WorkItem).filter_by(repo=repo, number=number, kind=kind).first()
    return item.id if item else None


@dataclass(frozen=True)
class HumanEdit:
    """A mirror-row field change attributable to a human (i.e. not something the
    reconciler itself just wrote) — the write-side echo-suppression counterpart
    to ``journal.recent_own_writes`` (spec 003 Phase B, issue #973 follow-on)."""
    ref: str
    field: str
    old: object
    new: object


def upsert_project_items(db: Session, gh_project_db_id: str,
                         items: list[GhProjectItemView], now: datetime, *,
                         own_writes: AbstractSet[tuple[str, str, str]] = frozenset()
                         ) -> list[HumanEdit]:
    """Upsert the project's items (prune stale rows) and classify every changed
    Status/Score/Stage field as a human edit UNLESS it matches an entry in
    ``own_writes`` — the reconciler's own recent applied writes, keyed with the
    shared ``journal.own_write_key`` helper (so ``480`` read back for a ``480.0``
    we wrote still matches) — in which case it's an echo of our own write landing
    back through the GitHub read path, not a human edit.

    A brand-new row (the item wasn't in the preloaded map) produces NO edits:
    there is no prior value to have changed, human or otherwise."""
    # Preload every existing mirror row once (keyed by node id) instead of a
    # per-item SELECT, and reuse the same dict to prune stale rows (issue #973).
    existing: dict[str, GhProjectItem] = {
        r.gh_item_node_id: r for r in db.query(GhProjectItem)
        .filter_by(gh_project_id=gh_project_db_id).all()
        if r.gh_item_node_id is not None}
    seen: set[str] = set()
    edits: list[HumanEdit] = []
    for view in items:
        seen.add(view.item_node_id)
        row = existing.get(view.item_node_id)
        is_new_row = row is None
        if row is None:
            row = GhProjectItem(gh_project_id=gh_project_db_id,
                                gh_item_node_id=view.item_node_id)
            db.add(row)
        if not is_new_row:
            for field, new in (("Status", view.status), ("Score", view.score),
                               ("Stage", view.stage)):
                old = getattr(row, field.lower())
                if old == new:
                    continue
                if own_write_key(view.item_node_id, field, new) not in own_writes:
                    edits.append(HumanEdit(f"{view.repo}#{view.number}", field, old, new))
        row.work_item_id = _work_item_id(db, view.repo, view.number, view.kind)
        row.status = view.status
        row.score = view.score
        row.stage = view.stage
        row.gh_updated_at = _parse_iso(view.updated_at)
        row.last_synced_at = now
    for node_id, row in existing.items():
        if node_id not in seen:
            db.delete(row)
    gh = db.get(GhProject, gh_project_db_id)
    if gh is not None:
        gh.last_synced_at = now
        gh.watermark = now
    return edits


def back_sync_stage(db: Session, edits: Sequence[HumanEdit]) -> int:
    """Apply human "Stage" edits back onto the linked Initiative (the mirror is
    read-only toward GitHub for issue fields, but Stage is the one field GitHub
    is allowed to drive — a human moving the board's Stage column back-syncs the
    initiative's lifecycle position). Only edits whose new value is a valid
    INITIATIVE_STAGES entry and whose ref resolves to an epic-linked Initiative
    are applied; returns the count of initiatives actually updated."""
    n = 0
    for e in edits:
        if e.field != "Stage" or e.new not in INITIATIVE_STAGES:
            continue
        repo, _, number = e.ref.rpartition("#")
        if not number.isdigit():
            continue
        ini = db.query(Initiative).filter_by(
            epic_repo=repo, epic_number=int(number)).first()
        if ini is not None and ini.stage != e.new:
            ini.stage = e.new
            n += 1
    return n


def upsert_relations(db: Session, repo: str, rels: list[IssueRelView]) -> None:
    by_ref: dict[str, WorkItem] = {
        f"{w.repo}#{w.number}": w
        for w in db.query(WorkItem).filter_by(repo=repo, kind="issue").all()}
    for rel in rels:
        src = by_ref.get(f"{rel.repo}#{rel.number}")
        if src is None:
            continue
        src.is_epic = bool(rel.sub_issue_refs)
        if rel.node_id:
            src.gh_node_id = rel.node_id
        parent = by_ref.get(rel.parent_ref) if rel.parent_ref else None
        src.parent_work_item_id = parent.id if parent else None
    bodies = {ref: item.body for ref, item in by_ref.items()
              if (item.state or "open") == "open"}
    observed = observed_deps(repo, rels, bodies)
    src_prefix = f"{repo}#"
    # Push the src_ref prefix filter into SQL instead of scanning the whole
    # WorkItemDep table in Python (issue #973). ``startswith`` auto-escapes the
    # repo string, so a repo name containing a LIKE wildcard (``_``) can't widen
    # the match.
    existing = list(db.query(WorkItemDep).filter(
        WorkItemDep.src_ref.startswith(src_prefix, autoescape=True)).all())
    plan = plan_dep_rows(observed, [
        DepRowView(id=d.id, src_ref=d.src_ref or "", dst_ref=d.dst_ref or "",
                   source=d.source or "", active=bool(d.active)) for d in existing])
    rows_by_id = {d.id: d for d in existing}
    for dep in plan.create:
        src_item = by_ref.get(dep.src_ref)
        db.add(WorkItemDep(
            src_ref=dep.src_ref, dst_ref=dep.dst_ref, source=dep.source, active=True,
            src_work_item_id=src_item.id if src_item else None,
            dst_work_item_id=_resolve_ref(db, dep.dst_ref)))
    for row_id in plan.reactivate:
        rows_by_id[row_id].active = True
    for row_id in plan.deactivate:
        rows_by_id[row_id].active = False
    # Backfill FKs left NULL at creation (the target wasn't mirrored yet). Runs
    # every pass over the repo's surviving rows, so a dep self-heals as soon as
    # repo-sync lands its endpoint — Phase C queue queries want these populated.
    for row in existing:
        if row.src_work_item_id is None:
            resolved = _resolve_ref(db, row.src_ref or "")
            if resolved is not None:
                row.src_work_item_id = resolved
        if row.dst_work_item_id is None:
            resolved = _resolve_ref(db, row.dst_ref or "")
            if resolved is not None:
                row.dst_work_item_id = resolved


def _resolve_ref(db: Session, ref: str) -> str | None:
    repo, _, number = ref.partition("#")
    if not number.isdigit():
        return None
    return _work_item_id(db, repo, int(number), "issue")
