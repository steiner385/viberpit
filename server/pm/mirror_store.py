"""Session-level persistence for the 003 read-only mirror. Callers own the transaction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from server.db.models import (
    GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

from .dep_plan import DepRowView, observed_deps, plan_dep_rows
from .gh_projects import GhProjectItemView, GhProjectView, IssueRelView


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


def upsert_project_items(db: Session, gh_project_db_id: str,
                         items: list[GhProjectItemView], now: datetime) -> None:
    seen: set[str] = set()
    for view in items:
        seen.add(view.item_node_id)
        row = db.query(GhProjectItem).filter_by(
            gh_project_id=gh_project_db_id, gh_item_node_id=view.item_node_id).first()
        if row is None:
            row = GhProjectItem(gh_project_id=gh_project_db_id,
                                gh_item_node_id=view.item_node_id)
            db.add(row)
        row.work_item_id = _work_item_id(db, view.repo, view.number, view.kind)
        row.status = view.status
        row.score = view.score
        row.stage = view.stage
        row.gh_updated_at = _parse_iso(view.updated_at)
        row.last_synced_at = now
    stale = db.query(GhProjectItem).filter_by(gh_project_id=gh_project_db_id).all()
    for row in stale:
        if row.gh_item_node_id not in seen:
            db.delete(row)
    gh = db.get(GhProject, gh_project_db_id)
    if gh is not None:
        gh.last_synced_at = now
        gh.watermark = now


def upsert_relations(db: Session, repo: str, rels: list[IssueRelView]) -> None:
    by_ref: dict[str, WorkItem] = {
        f"{w.repo}#{w.number}": w
        for w in db.query(WorkItem).filter_by(repo=repo, kind="issue").all()}
    for rel in rels:
        src = by_ref.get(f"{rel.repo}#{rel.number}")
        if src is None:
            continue
        src.is_epic = bool(rel.sub_issue_refs)
        parent = by_ref.get(rel.parent_ref) if rel.parent_ref else None
        src.parent_work_item_id = parent.id if parent else None
    bodies = {ref: item.body for ref, item in by_ref.items()
              if (item.state or "open") == "open"}
    observed = observed_deps(repo, rels, bodies)
    src_prefix = f"{repo}#"
    existing = [d for d in db.query(WorkItemDep).all()
                if (d.src_ref or "").startswith(src_prefix)]
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


def _resolve_ref(db: Session, ref: str) -> str | None:
    repo, _, number = ref.partition("#")
    if not number.isdigit():
        return None
    return _work_item_id(db, repo, int(number), "issue")
