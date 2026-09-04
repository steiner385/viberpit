"""Session-level persistence for the 003 read-only mirror. Callers own the transaction."""
from __future__ import annotations

from collections.abc import Collection, Sequence, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import false, or_, update
from sqlalchemy.orm import Session

from server.db.models import (
    INITIATIVE_STAGES, GhProject, GhProjectItem, Initiative, Project, ProjectRepo,
    TriageProposal, WorkItem, WorkItemDep)
from server.sdlc.slug import slugify

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


def deps_in_repos(repos: Collection[str]):
    """SQL filter for ``WorkItemDep`` rows whose SOURCE issue lives in *repos*.

    THE one definition of "this product's dep edges" (issue #973): the deps API and
    the reconciler's facts loader both scope by it, and a Python-side scan of the
    whole table instead grows with every product ccswitch has ever onboarded, not
    with the one being converged. ``startswith`` auto-escapes each repo string so a
    LIKE wildcard in a repo name (``_`` is legal in a GitHub repo name) can't widen
    the match. No repos ⇒ ``false()``, never an empty ``or_()`` (which is a no-op
    clause and would select the whole table)."""
    if not repos:
        return false()
    return or_(*[WorkItemDep.src_ref.startswith(f"{r}#", autoescape=True)
                 for r in sorted(repos)])


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


def _unique_slug(db: Session, base: str) -> str:
    """*base* if free, else the first ``base-N`` (N starting at 2) not already
    taken by an Initiative — mirrors the collision behavior a human hits at
    ``POST /api/sdlc/initiatives`` (409 slug_conflict), except adoption just
    picks the next free one instead of failing."""
    slug = base
    n = 2
    while db.query(Initiative).filter_by(slug=slug).first() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def adopt_epics(db: Session, project_id: str, *, now: datetime) -> list[str]:
    """Create an Initiative overlay for every open, board-linked epic this
    product doesn't already have one for (issue #991). ``stage`` is seeded
    from the board's own mirrored ``GhProjectItem.stage`` when that value is a
    valid ``INITIATIVE_STAGES`` entry, else ``"build"`` — GitHub is allowed to
    drive an Initiative's Stage once one exists (``back_sync_stage`` above), so
    adoption must not stamp every epic ``"build"`` and have the very next
    gh-reconcile tick push that value back onto a board Stage a human had
    deliberately set to e.g. Design or Plan (issue #991 final review, R7).

    "On the board" = a ``GhProjectItem`` whose ``gh_project_id`` belongs to one
    of this product's ``GhProject`` rows; "epic" = ``WorkItem.is_epic`` on an
    open (``state == "open"``) item. A work item can be mirrored by more than
    one ``GhProjectItem`` row for this product (e.g. two of its boards both
    carry the same GitHub issue); resolved by loading rows ordered by
    ``updated_at`` ascending and overwriting a plain dict as we go, so the
    MOST RECENTLY updated row's Stage wins — the same "last write wins" idiom
    ``server.api.sdlc._epic_progress_batch`` uses to resolve a child's status
    across multiple board rows. An epic already covered by an Initiative with
    the matching ``(epic_repo, epic_number)`` is left alone — idempotent, a
    second call adopts nothing already covered. That "already adopted" check
    is keyed globally on ``(epic_repo, epic_number)`` with no ``project_id``
    filter: if the same epic is mirrored on two different products' boards,
    whichever product's sync reaches it first owns the adopted row's
    ``project_id`` — first-writer-wins, not duplicated. Returns the
    ``repo#number`` refs of the epics adopted THIS call, in no particular
    order."""
    board_ids = [g.id for g in db.query(GhProject).filter_by(project_id=project_id).all()]
    if not board_ids:
        return []
    items = (
        db.query(GhProjectItem.work_item_id, GhProjectItem.stage)
        .filter(GhProjectItem.gh_project_id.in_(board_ids))
        .order_by(GhProjectItem.updated_at)
        .all())
    stage_by_work_item: dict[str, str | None] = {}
    for work_item_id, stage in items:
        if work_item_id is not None:
            stage_by_work_item[work_item_id] = stage
    if not stage_by_work_item:
        return []
    epics = db.query(WorkItem).filter(
        WorkItem.id.in_(stage_by_work_item), WorkItem.is_epic.is_(True),
        WorkItem.state == "open").all()
    adopted: list[str] = []
    for epic in epics:
        if not epic.repo or epic.number is None:
            continue
        already = db.query(Initiative).filter_by(
            epic_repo=epic.repo, epic_number=epic.number).first()
        if already is not None:
            continue
        ref = f"{epic.repo}#{epic.number}"
        title = epic.title or ref
        board_stage = stage_by_work_item.get(epic.id)
        stage = board_stage if board_stage in INITIATIVE_STAGES else "build"
        db.add(Initiative(
            slug=_unique_slug(db, slugify(title)), repo=epic.repo, title=title,
            stage=stage, status="active", origin="epic-adoption", origin_ref=ref,
            epic_repo=epic.repo, epic_number=epic.number, project_id=project_id,
            created_at=now, updated_at=now))
        adopted.append(ref)
    return adopted


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


def _rowcount(result: object) -> int:
    """Rows affected by a bulk ``update()``; typed as ``Result`` by SQLAlchemy's
    stubs, which don't expose ``rowcount``, so read it defensively."""
    return int(getattr(result, "rowcount", 0) or 0)


def rename_repo(db: Session, old: str, new: str) -> dict[str, int]:
    """Re-key every mirror row from alias *old* to GitHub's canonical *new*.

    The single data-correction path for issue #999: ``ProjectRepo.repo`` was
    entered by hand as a pre-rename / differently-cased alias, GitHub kept
    answering queries for it, and every mirrored key (``WorkItem.repo``, the
    ``repo#N`` refs on ``WorkItemDep``, the Initiative pointers) followed the
    alias while GitHub-sourced refs stayed canonical — so no parent linked and no
    native edge resolved. pm-sync calls this the moment a payload's
    ``nameWithOwner`` disagrees with the stored name, before writing any row
    from that payload; it is idempotent (nothing under *old* ⇒ all-zero counts)
    and a no-op when the names already agree.

    Collisions: a canonical twin that already exists (the repo onboarded under
    both names) wins — the alias ``WorkItem`` is dropped after everything that
    pointed at it is re-pointed at the twin, and a dep edge whose rewritten
    ``(src, dst, source)`` already exists is dropped rather than tripping the
    unique key. Ends by re-resolving the NULL FKs that only existed because the
    two halves of the graph disagreed. Caller owns the transaction."""
    if not old or not new or old == new:
        return {}
    counts = {"project_repos": 0, "work_items": 0, "merged": 0, "deps": 0,
              "deps_dropped": 0, "initiatives": 0, "fks_resolved": 0}

    # 1. ProjectRepo: rename, or drop the alias row when the project already
    #    carries the canonical name (uq_project_repo).
    canonical_repo_projects = {r.project_id for r in db.query(ProjectRepo).filter_by(repo=new)}
    for row in db.query(ProjectRepo).filter_by(repo=old).all():
        if row.project_id in canonical_repo_projects:
            db.delete(row)
        else:
            row.repo = new
            counts["project_repos"] += 1

    # 2. WorkItem: bulk rename every alias row without a canonical twin; merge
    #    the rest into their twin (re-point the FKs, then drop the alias row).
    twins = {(number, kind): wid for wid, number, kind in
             db.query(WorkItem.id, WorkItem.number, WorkItem.kind).filter_by(repo=new)}
    twin_of: dict[str, str] = {}
    for wid, number, kind in db.query(WorkItem.id, WorkItem.number, WorkItem.kind).filter_by(repo=old):
        twin = twins.get((number, kind))
        if twin is not None:
            twin_of[wid] = twin
    alias_ids = list(twin_of)
    if alias_ids:
        for w in db.query(WorkItem).filter(WorkItem.parent_work_item_id.in_(alias_ids)).all():
            w.parent_work_item_id = twin_of[w.parent_work_item_id or ""]
        for it in db.query(GhProjectItem).filter(GhProjectItem.work_item_id.in_(alias_ids)).all():
            it.work_item_id = twin_of[it.work_item_id or ""]
        for tp in db.query(TriageProposal).filter(TriageProposal.work_item_id.in_(alias_ids)).all():
            tp.work_item_id = twin_of[tp.work_item_id]
        for d in db.query(WorkItemDep).filter(or_(
                WorkItemDep.src_work_item_id.in_(alias_ids),
                WorkItemDep.dst_work_item_id.in_(alias_ids))).all():
            d.src_work_item_id = twin_of.get(d.src_work_item_id or "", d.src_work_item_id)
            d.dst_work_item_id = twin_of.get(d.dst_work_item_id or "", d.dst_work_item_id)
        db.flush()
        for alias_id in alias_ids:
            db.delete(db.get(WorkItem, alias_id))
        counts["merged"] = len(alias_ids)
        db.flush()
    counts["work_items"] = _rowcount(db.execute(
        update(WorkItem).where(WorkItem.repo == old).values(repo=new)))

    # 3. WorkItemDep refs: prefix rewrite on either end, deduped against the
    #    edges that are already canonical (uq_work_item_dep).
    old_prefix, new_prefix = f"{old}#", f"{new}#"

    def _rekey(ref: str | None) -> str | None:
        if ref and ref.startswith(old_prefix):
            return new_prefix + ref[len(old_prefix):]
        return ref

    seen: set[tuple[str | None, str | None, str | None]] = set()
    touched: list[WorkItemDep] = []
    for d in db.query(WorkItemDep).all():
        if (d.src_ref or "").startswith(old_prefix) or (d.dst_ref or "").startswith(old_prefix):
            touched.append(d)
        else:
            seen.add((d.src_ref, d.dst_ref, d.source))
    for d in touched:
        key = (_rekey(d.src_ref), _rekey(d.dst_ref), d.source)
        if key in seen:
            db.delete(d)
            counts["deps_dropped"] += 1
            continue
        seen.add(key)
        d.src_ref, d.dst_ref = key[0], key[1]
        counts["deps"] += 1

    # 4. Initiative pointers (both the owning repo and the epic pointer).
    counts["initiatives"] = (
        _rowcount(db.execute(update(Initiative).where(Initiative.repo == old).values(repo=new)))
        + _rowcount(db.execute(update(Initiative).where(Initiative.epic_repo == old)
                               .values(epic_repo=new))))
    db.flush()

    # 5. The FKs that were NULL only because src was alias and dst canonical (or
    #    vice versa) resolve now that both halves share one name.
    for d in db.query(WorkItemDep).filter(or_(
            WorkItemDep.src_ref.startswith(new_prefix, autoescape=True),
            WorkItemDep.dst_ref.startswith(new_prefix, autoescape=True))).all():
        for attr, ref in (("src_work_item_id", d.src_ref), ("dst_work_item_id", d.dst_ref)):
            if getattr(d, attr) is None:
                resolved = _resolve_ref(db, ref or "")
                if resolved is not None:
                    setattr(d, attr, resolved)
                    counts["fks_resolved"] += 1
    db.flush()
    return counts
