"""SDLC initiative API (#802) + build-stage correlation (#801) + the Phase-2
control-band triage queue (#804)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    TRIAGE_STATUSES,
    Feature,
    GhProjectItem,
    Initiative,
    InitiativeArtifact,
    InitiativeFeature,
    MaterializationProposal,
    Project,
    Session as DbSession,
    TriageItem,
    WorkItem,
)
from server.pm.desired_state import STATUS_BLOCKED, STATUS_NEEDS_HUMAN
from server.pm.priority import initiative_priority
from server.pm.score import rice_score
from server.pm.tree import derived_stage, rollup_epic_progress
from server.sdlc import triage_store
from server.sdlc.engine import (
    Advance,
    ArtifactView,
    AwaitApproval,
    Done,
    HandoffToBuild,
    InitiativeView,
    Materialize,
    MergeArtifact,
    NeedsHuman,
    Propose,
    advance_stage,
    next_action,
    step_back_stage,
)
from server.sdlc.slug import slugify

router = APIRouter(prefix="/api/sdlc", tags=["sdlc"])


def _initiative_to_dict(i: Initiative) -> dict:
    return {
        "id": i.id,
        "slug": i.slug,
        "repo": i.repo,
        "title": i.title,
        "stage": i.stage,
        "status": i.status,
        "origin": i.origin,
        "originRef": i.origin_ref,
        "epicRepo": i.epic_repo,
        "epicNumber": i.epic_number,
        "lastAutoGate": i.last_auto_gate,
        "createdAt": i.created_at.isoformat() if i.created_at else None,
        "updatedAt": i.updated_at.isoformat() if i.updated_at else None,
    }


async def _project_slugs(db, initiatives: list[Initiative]) -> dict[str, str | None]:
    """initiative.id → owning product slug (None when unlinked)."""
    ids = {i.project_id for i in initiatives if i.project_id}
    slug_by_project: dict[str, str | None] = {}
    if ids:
        slug_by_project = {p.id: p.slug for p in (await db.execute(
            select(Project).where(Project.id.in_(ids)))).scalars().all()}
    return {i.id: slug_by_project.get(i.project_id) if i.project_id else None for i in initiatives}


async def _capabilities_batch(db, initiatives: list[Initiative]) -> dict[str, tuple[list[dict], dict | None]]:
    """initiative.id → ([{id, slug, title, isPrimary}…], primary {id, slug, title} | None),
    in a fixed two queries regardless of how many initiatives are passed."""
    result: dict[str, tuple[list[dict], dict | None]] = {i.id: ([], None) for i in initiatives}
    if not initiatives:
        return result
    rows = (await db.execute(select(InitiativeFeature).where(
        InitiativeFeature.initiative_id.in_([i.id for i in initiatives])))).scalars().all()
    if not rows:
        return result
    features = {f.id: f for f in (await db.execute(select(Feature).where(
        Feature.id.in_({r.feature_id for r in rows})))).scalars().all()}
    for r in rows:
        f = features.get(r.feature_id)
        if f is None:
            continue
        entry = {"id": f.id, "slug": f.slug, "title": f.title, "isPrimary": bool(r.is_primary)}
        caps, primary = result[r.initiative_id]
        caps.append(entry)
        if r.is_primary:
            primary = {"id": f.id, "slug": f.slug, "title": f.title}
        result[r.initiative_id] = (caps, primary)
    return result


async def _nesting_batch(db, initiatives: list[Initiative]) -> tuple[dict[str, str | None], dict[str, list[Initiative]]]:
    """(id → parent slug, id → direct phases) in two queries."""
    ids = [i.id for i in initiatives]
    if not ids:
        return {}, {}
    parent_ids = {i.parent_initiative_id for i in initiatives if i.parent_initiative_id}
    parents = {p.id: p for p in (await db.execute(select(Initiative).where(
        Initiative.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}
    kids = (await db.execute(select(Initiative).where(
        Initiative.parent_initiative_id.in_(ids)).order_by(Initiative.created_at))).scalars().all()
    by_parent: dict[str, list[Initiative]] = {}
    for k in kids:
        by_parent.setdefault(k.parent_initiative_id, []).append(k)
    parent_slug = {i.id: (parents[i.parent_initiative_id].slug if i.parent_initiative_id in parents else None)
                   for i in initiatives}
    return parent_slug, by_parent


async def _descendants(db, roots: list[Initiative]) -> list[Initiative]:
    """Every initiative below *roots*, breadth-first (GitHub nests to 8 levels)."""
    out: list[Initiative] = []
    frontier = [r.id for r in roots]
    seen = set(frontier)
    while frontier:
        rows = (await db.execute(select(Initiative).where(
            Initiative.parent_initiative_id.in_(frontier)))).scalars().all()
        frontier = [r.id for r in rows if r.id not in seen]
        seen.update(frontier)
        out.extend(rows)
    return out


async def _progress_rolled_up(db, initiatives: list[Initiative], *, include_children: bool) -> dict[str, dict | None]:
    """`_epic_progress_batch` over *initiatives* plus every descendant, rolled
    up so a program reports its leaf issues (tree.rollup_epic_progress)."""
    extra = await _descendants(db, initiatives)
    rows = list({i.id: i for i in [*initiatives, *extra]}.values())
    entries = await _epic_progress_batch(db, rows, include_children=include_children)
    parent_of = {i.id: i.parent_initiative_id for i in rows}
    return rollup_epic_progress(entries, parent_of)


async def _decorate(db, initiatives: list[Initiative], dicts: list[dict]) -> None:
    """Attach projectSlug / primaryCapability / capabilities to already-built
    dicts, plus nesting (parentInitiativeSlug / hasChildren / children) and a
    program's derived stage (the earliest non-done phase stage)."""
    slugs = await _project_slugs(db, initiatives)
    caps = await _capabilities_batch(db, initiatives)
    parent_slug, by_parent = await _nesting_batch(db, initiatives)
    for i, d in zip(initiatives, dicts):
        d["projectSlug"] = slugs.get(i.id)
        d["capabilities"], d["primaryCapability"] = caps[i.id]
        phases = by_parent.get(i.id, [])
        d["parentInitiativeSlug"] = parent_slug.get(i.id)
        d["hasChildren"] = bool(phases)
        d["children"] = [{"slug": p.slug, "title": p.title, "stage": p.stage, "status": p.status} for p in phases]
        if phases:
            # None (no phase carries a recognized stage) keeps the row's own
            # stage rather than overwriting it with a phantom value.
            d["stage"] = derived_stage([p.stage for p in phases]) or i.stage


async def _epic_progress_batch(
    db, initiatives: list[Initiative], *, include_children: bool = True,
) -> dict[str, dict | None]:
    """Sub-issue progress for every initiative that points at an epic (spec 003
    §7c), computed in a fixed small number of grouped queries — not one call per
    initiative — so `GET /initiatives` can carry it on every row without an N+1
    (the same idiom `list_initiatives` already uses to batch artifacts). ViberPit
    deep-links to the GitHub epic rather than rebuilding a board, so ``url``
    matters as much as the counts.

    A child WorkItem is counted by ``WorkItem.id`` regardless of how many
    ``GhProjectItem`` rows reference it — nothing constrains a work item to a
    single project board, so an outer join alone can silently double-count a
    child mirrored onto more than one board. When more than one status row
    exists for a child, the most recently updated one wins.

    ``include_children`` (default ``True``) controls whether the per-child
    array is serialized into each entry. `list_initiatives` (the LIST
    endpoint) passes ``False``: its readers (the AI assistant's
    `server/assistant/tools.py`, and anything scripting the API) only need the
    epic's `url`/`done`/`total`/`blocked`/`needsHuman`, so an unread
    one-object-per-sub-issue array would be serialized for no reader. The web
    roadmap does not call it at all — the map endpoint (`/api/pm/map`) already
    carries per-initiative progress, and the roadmap's initiative page fetches
    the per-slug DETAIL route. `get_initiative` (that detail endpoint) keeps
    the default. One helper either way, so the two endpoints can't drift apart
    on the counts themselves (constitution VI)."""
    result: dict[str, dict | None] = {i.id: None for i in initiatives}
    pointers = [(i.id, i.epic_repo, i.epic_number) for i in initiatives
                if i.epic_repo and i.epic_number]
    if not pointers:
        return result

    repos = {r for _, r, _ in pointers}
    numbers = {n for _, _, n in pointers}
    epic_rows = (
        await db.execute(
            select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.number.in_(numbers))
        )
    ).scalars().all()
    epic_by_pointer = {(w.repo, w.number): w for w in epic_rows}

    epic_ids = [w.id for w in epic_by_pointer.values()]
    children_by_parent: dict[str, list[WorkItem]] = {}
    status_by_child: dict[str, str | None] = {}
    if epic_ids:
        children = (
            await db.execute(
                select(WorkItem).where(WorkItem.parent_work_item_id.in_(epic_ids))
            )
        ).scalars().all()
        for c in children:
            children_by_parent.setdefault(c.parent_work_item_id, []).append(c)
        child_ids = [c.id for c in children]
        if child_ids:
            status_rows = (
                await db.execute(
                    select(GhProjectItem.work_item_id, GhProjectItem.status)
                    .where(GhProjectItem.work_item_id.in_(child_ids))
                    .order_by(GhProjectItem.updated_at)
                )
            ).all()
            for wid, st in status_rows:
                status_by_child[wid] = st

    for init_id, repo, number in pointers:
        epic_wi = epic_by_pointer.get((repo, number))
        children = children_by_parent.get(epic_wi.id, []) if epic_wi is not None else []
        entry = {
            "ref": f"{repo}#{number}",
            "url": f"https://github.com/{repo}/issues/{number}",
            "total": len(children),
            "done": sum(1 for c in children if c.state == "closed"),
            "blocked": sum(1 for c in children if status_by_child.get(c.id) == STATUS_BLOCKED),
            "needsHuman": sum(1 for c in children if status_by_child.get(c.id) == STATUS_NEEDS_HUMAN),
            # Spec 2026-09-14 D6: computed here, once, for every reader.
            "priority": initiative_priority(
                rice_score(epic_wi.reach, epic_wi.impact, epic_wi.effort, epic_wi.confidence)
                if epic_wi is not None else None,
                (rice_score(c.reach, c.impact, c.effort, c.confidence)
                 for c in children if c.state == "open")),
        }
        if include_children:
            entry["children"] = [
                {"ref": f"{c.repo}#{c.number}", "title": c.title, "state": c.state,
                 "status": status_by_child.get(c.id)}
                for c in children
            ]
        result[init_id] = entry
    return result


def _artifact_to_dict(a: InitiativeArtifact) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "repoPath": a.repo_path,
        "commitSha": a.commit_sha,
        "prNumber": a.pr_number,
        "prUrl": a.pr_url,
        "sessionId": a.session_id,
        "model": a.model,
        "createdAt": a.created_at.isoformat() if a.created_at else None,
    }


def _action_to_dict(action: object) -> dict:
    if isinstance(action, Propose):
        return {"type": "propose", "kind": action.kind}
    if isinstance(action, AwaitApproval):
        return {"type": "await_approval", "kind": action.kind}
    if isinstance(action, Materialize):
        return {"type": "materialize", "kind": action.kind}
    if isinstance(action, HandoffToBuild):
        return {"type": "handoff_to_build", "issue": action.issue}
    if isinstance(action, Done):
        return {"type": "done"}
    if isinstance(action, NeedsHuman):
        return {"type": "needs_human", "reason": action.reason}
    if isinstance(action, MergeArtifact):
        return {"type": "merge_artifact", "kind": action.kind, "prNumber": action.pr_number}
    if isinstance(action, Advance):
        return {"type": "advance", "toStage": action.to_stage}
    return {"type": "unknown"}  # pragma: no cover — union is closed


async def _resolve_capabilities(
    db, primary_id: str | None, capability_ids: list[str],
) -> tuple[list[str], dict[str, Feature]]:
    """The capability set an initiative may link: ``capability_ids`` plus the
    primary (which always belongs to the set), de-duplicated in order; every id
    must exist and all must sit under ONE product. Shared by create and PATCH so
    the two cannot drift (constitution VI). Returns ``(wanted_ids, features)``."""
    wanted = list(dict.fromkeys(capability_ids + ([primary_id] if primary_id else [])))
    features: dict[str, Feature] = {}
    if wanted:
        features = {f.id: f for f in (await db.execute(
            select(Feature).where(Feature.id.in_(wanted)))).scalars().all()}
        missing = [fid for fid in wanted if fid not in features]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "capability_not_found",
                        "message": f"unknown capability id(s): {', '.join(missing)}"})
        if len({f.project_id for f in features.values()}) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "capabilities_span_products",
                        "message": "all capabilities must belong to one product"})
    return wanted, features


def _project_of(wanted: list[str], features: dict[str, Feature], primary_id: str | None) -> str | None:
    """The product an initiative belongs to: its primary's, else its first capability's."""
    if not wanted:
        return None
    anchor = features[primary_id] if primary_id else features[wanted[0]]
    return anchor.project_id


class InitiativeCreateBody(BaseModel):
    title: str
    repo: str
    slug: str | None = None
    origin: str | None = None
    originRef: str | None = None
    primaryCapabilityId: str | None = None
    capabilityIds: list[str] = []


@router.get("/initiatives")
async def list_initiatives(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Return all initiatives ordered by created_at, each with its committed
    artifact chain and (for those adopted from a GitHub epic) sub-issue
    progress. Both are fetched in a fixed number of grouped queries so the web
    board renders N tracks (#803) from a single call rather than a detail fetch
    per row (the detail endpoint additionally computes nextAction + build
    sessions, which the board doesn't need per row) — this is the only place
    the web board reads from, so epic progress has to live here, not only on
    the per-slug detail endpoint (#991)."""
    rows = (
        await db.execute(select(Initiative).order_by(Initiative.created_at))
    ).scalars().all()
    if not rows:
        return []
    ids = [i.id for i in rows]
    artifacts = (
        await db.execute(
            select(InitiativeArtifact)
            .where(InitiativeArtifact.initiative_id.in_(ids))
            .order_by(InitiativeArtifact.created_at)
        )
    ).scalars().all()
    by_init: dict[str, list[InitiativeArtifact]] = {}
    for a in artifacts:
        by_init.setdefault(a.initiative_id, []).append(a)
    epics = await _progress_rolled_up(db, rows, include_children=False)
    result = []
    for i in rows:
        d = _initiative_to_dict(i)
        d["artifacts"] = [_artifact_to_dict(a) for a in by_init.get(i.id, [])]
        d["epic"] = epics.get(i.id)
        d["priority"] = (d["epic"] or {}).get("priority")
        result.append(d)
    await _decorate(db, rows, result)
    return result


@router.post("/initiatives", status_code=status.HTTP_201_CREATED)
async def create_initiative(
    body: InitiativeCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create an initiative. Returns 409 if slug already exists."""
    slug = body.slug or slugify(body.title)
    existing = (
        await db.execute(select(Initiative).where(Initiative.slug == slug))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_conflict", "message": f"Initiative '{slug}' already exists"},
        )

    wanted, features = await _resolve_capabilities(db, body.primaryCapabilityId, body.capabilityIds)
    project_id = _project_of(wanted, features, body.primaryCapabilityId)

    ini = Initiative(
        slug=slug,
        repo=body.repo,
        title=body.title,
        origin=body.origin,
        origin_ref=body.originRef,
        project_id=project_id,
    )
    db.add(ini)
    await db.flush()
    for fid in wanted:
        db.add(InitiativeFeature(initiative_id=ini.id, feature_id=fid,
                                 is_primary=(fid == body.primaryCapabilityId)))
    await db.flush()
    await record_audit(
        db=db,
        type="sdlc.initiative.create",
        identity=identity,
        detail={"slug": slug, "title": body.title},
    )
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


@router.get("/initiatives/{slug}")
async def get_initiative(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return initiative detail including artifacts, buildSessions, and nextAction."""
    ini = (
        await db.execute(select(Initiative).where(Initiative.slug == slug))
    ).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    artifacts = (
        await db.execute(
            select(InitiativeArtifact)
            .where(InitiativeArtifact.initiative_id == ini.id)
            .order_by(InitiativeArtifact.created_at)
        )
    ).scalars().all()

    # Build nextAction via the pure engine: display-only: mode on, tolerance
    # cautious, so the detail shows the human's next step
    state = InitiativeView(
        slug=ini.slug or "",
        stage=ini.stage,
        status=ini.status,
        repo=ini.repo or "",
        issue=ini.origin_ref,
        # Same fold as the synthesizer's projection: an epic pointer means the
        # plan has already been materialized (spec 003 §7b). NOT the wire `epic`
        # key below, which is the epic *progress* object.
        epic=f"{ini.epic_repo}#{ini.epic_number}" if ini.epic_number else None,
    )
    artifact_views = [
        ArtifactView(
            kind=a.kind or "",
            commit_sha=a.commit_sha,
            pr_number=a.pr_number,
            pr_url=a.pr_url,
        )
        for a in artifacts
    ]
    action = next_action(state, artifact_views, "on", "cautious")

    # Build-stage correlation (#801): find backlog-runner sessions with same origin_ref
    build_sessions: list[dict] = []
    if ini.origin_ref:
        session_rows = (
            await db.execute(
                select(DbSession).where(
                    DbSession.origin == "backlog-runner",
                    DbSession.origin_ref == ini.origin_ref,
                )
            )
        ).scalars().all()
        build_sessions = [
            {"id": s.id, "status": s.status, "branch": s.branch}
            for s in session_rows
        ]

    result = _initiative_to_dict(ini)
    result["artifacts"] = [_artifact_to_dict(a) for a in artifacts]
    result["buildSessions"] = build_sessions
    result["nextAction"] = _action_to_dict(action)
    result["epic"] = (await _progress_rolled_up(db, [ini], include_children=True))[ini.id]
    result["priority"] = (result["epic"] or {}).get("priority")
    await _decorate(db, [ini], [result])
    # A program (an initiative with phases) is never driven directly — advance
    # and step-back both refuse it (409 has_children above), so its nextAction
    # is meaningless and must not point at a stage transition nobody can take.
    if result["hasChildren"]:
        result["nextAction"] = None
    return result


@router.post("/initiatives/{slug}/advance")
async def advance_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Advance the initiative to the next stage. 409 when already done or
    blocked — a blocked row is cleared on purpose (``unblock``), never by
    moving the gate over it."""
    ini = await _load_initiative(db, slug)
    if (await db.execute(select(Initiative.id).where(
            Initiative.parent_initiative_id == ini.id))).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "has_children",
            "message": "a program's stage is derived from its phases; advance a phase instead"})
    if ini.stage == "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_done", "message": "Initiative is already done"},
        )
    if ini.status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "blocked",
                    "message": "Clear the block first (unblock, or accept/reject "
                               "the pending materialization)"},
        )
    ini.stage = advance_stage(str(ini.stage or "intake"))
    ini.status = "active"
    await db.flush()
    await record_audit(
        db=db,
        type="sdlc.initiative.advance",
        identity=identity,
        detail={"slug": slug, "stage": ini.stage},
    )
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


async def _load_initiative(db, slug: str) -> Initiative:
    ini = (await db.execute(select(Initiative).where(Initiative.slug == slug))).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return ini


@router.post("/initiatives/{slug}/step-back")
async def step_back_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Move the human gate BACK one stage (roadmap spec §8, §10): the undo for a
    mis-tapped Advance. Status becomes ``active`` exactly as advance does, so the
    stage engine re-enters the earlier stage on its next tick. 409 at the first
    stage, and 409 while blocked: a block is cleared by ``unblock``, not walked
    around in either direction."""
    ini = await _load_initiative(db, slug)
    if (await db.execute(select(Initiative.id).where(
            Initiative.parent_initiative_id == ini.id))).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "has_children",
            "message": "a program's stage is derived from its phases; advance a phase instead"})
    if ini.status == "blocked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "blocked", "message": "Clear the block first"})
    prev = step_back_stage(str(ini.stage or "intake"))
    if prev is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "at_first_stage", "message": "Initiative is already at the first stage"})
    ini.stage = prev
    ini.status = "active"
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.step_back", identity=identity,
                       detail={"slug": slug, "stage": prev})
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


class InitiativePatchBody(BaseModel):
    title: str | None = None
    primaryCapabilityId: str | None = None
    capabilityIds: list[str] | None = None


@router.patch("/initiatives/{slug}")
async def update_initiative(
    slug: str,
    body: InitiativePatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Inline edit of what a human owns on an initiative (roadmap spec §8, §10):
    the title, and which capabilities it is primary to / touches.

    ``capabilityIds`` REPLACES the touched set; the primary always belongs to
    the set (as on create). A primary given alone re-primaries inside the
    current set; a set given alone keeps the current primary while it is still
    a member and drops it otherwise. The product is re-derived from the primary
    (else the first capability) — ``None`` when the set empties — because the
    initiative's product IS its primary capability's product (§10 model). An
    empty body is a read-through: nothing to audit.

    ``primaryCapabilityId: ""`` is normalised to ``None`` — "not chosen", never
    "clear the primary". A blank select value must not orphan an initiative:
    clearing the primary clears the product too, and ``pm_tree`` selects
    initiatives by product, so a product-less one appears NOWHERE on the map.
    Emptying the touched set is expressible on its own (``capabilityIds: []``)."""
    ini = await _load_initiative(db, slug)
    if body.primaryCapabilityId == "":
        body.primaryCapabilityId = None
    fields = body.model_dump(exclude_none=True)
    if not fields:
        d = _initiative_to_dict(ini)
        await _decorate(db, [ini], [d])
        return d
    # The audit carries the VALUES written, not just which fields moved — every
    # sibling audit here records what it wrote, and "fields: [title]" cannot
    # answer "what was it renamed to?".
    detail: dict[str, Any] = {"slug": slug, "fields": sorted(fields)}
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
                "error": "bad_title", "message": "title must not be blank"})
        ini.title = title
        detail["title"] = title
    if body.primaryCapabilityId is not None or body.capabilityIds is not None:
        # Serialize concurrent PATCHes of this initiative: the delete → flush →
        # re-add below is read-modify-write, so under READ COMMITTED two
        # overlapping requests both delete and the loser's INSERT then violates
        # `uq_initiative_feature` (a 500). A no-op on SQLite, which has one writer.
        await db.execute(select(Initiative).where(Initiative.id == ini.id).with_for_update())
        current = (await db.execute(select(InitiativeFeature).where(
            InitiativeFeature.initiative_id == ini.id))).scalars().all()
        current_primary = next((r.feature_id for r in current if r.is_primary), None)
        ids = body.capabilityIds if body.capabilityIds is not None else [r.feature_id for r in current]
        if body.primaryCapabilityId is not None:
            primary: str | None = body.primaryCapabilityId
        else:
            primary = current_primary if current_primary in ids else None
        wanted, features = await _resolve_capabilities(db, primary, ids)
        for r in current:
            await db.delete(r)
        await db.flush()
        for fid in wanted:
            db.add(InitiativeFeature(initiative_id=ini.id, feature_id=fid, is_primary=(fid == primary)))
        ini.project_id = _project_of(wanted, features, primary)
        detail["primaryCapabilityId"] = primary
        detail["capabilityIds"] = wanted
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.update", identity=identity, detail=detail)
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


@router.post("/initiatives/{slug}/unblock")
async def unblock_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Clear a ``blocked`` initiative back to ``awaiting_human`` — the parked
    state the plan stage already uses (see ``reject_materialization``) — without
    touching the stage. Before this route a blocked row was a dead end
    (``next_action`` answers NeedsHuman for it and nothing offered a way out).

    ``blocked`` has TWO writers, and only one of them is clearable here:
    a failed materialize apply (``server/pm/materialize_apply.py:fail``), and the
    synthesizer's exhaustion path (``sdlc_synthesizer._materialize``), which
    blocks a plan whose decomposition has failed ``MATERIALIZE_MAX_ATTEMPTS``
    times.

    409 ``proposal_pending``: a pending proposal already carries its own two
    dispositions (accept resumes the apply, reject abandons it) and the card for
    them stays up; clearing the row underneath would tell the loop to decompose
    again while a human is still deciding.

    409 ``materialize_exhausted``: the synthesizer wrote this block and its
    warn is latched in-process, so clearing it would send the row back to
    ``awaiting_human`` only for the next tick to re-block it silently. Resetting
    the attempt count (removing the stale proposals) is the way out; a retry path
    for that is a follow-up.

    The web confirm says out loud that a re-decomposition creates a second epic if
    a failed apply's issues are still open on GitHub — closing those is the
    human's call, and this route does not touch GitHub."""
    ini = await _load_initiative(db, slug)
    if ini.status != "blocked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_blocked", "message": f"Initiative is {ini.status!r}, not blocked"})
    pending = (await db.execute(select(MaterializationProposal.id).where(
        MaterializationProposal.initiative_id == ini.id,
        MaterializationProposal.status == "pending"))).scalars().first()
    if pending is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "proposal_pending",
            "message": "Accept or reject the pending materialization first"})
    if ini.stage == "plan" and ini.epic_number is None:
        # Imported inside the handler: the orchestrator module pulls in the whole
        # session stack, which a router must not do at import time. The count
        # itself is the loop's own — one implementation of "attempts spent".
        from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS
        from server.sessions.orchestrator import SessionOrchestrator
        attempts = await SessionOrchestrator._sdlc_materialization_attempts(db, ini.id)
        if attempts >= MATERIALIZE_MAX_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "materialize_exhausted",
                "message": f"Decomposition was attempted {attempts} times and gave "
                           "up; clearing the block would only re-block it on the "
                           "next tick. Reject or remove the stale proposals first."})
    ini.status = "awaiting_human"
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.unblock", identity=identity,
                       detail={"slug": slug})
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


# --------------------------------------------------------------------------- #
# Plan materialization — the human gate (spec 003 §7b, #992)                    #
# --------------------------------------------------------------------------- #


def _materialization_to_dict(p: MaterializationProposal) -> dict:
    payload = p.payload or {}
    return {
        "id": p.id,
        "initiativeId": p.initiative_id,
        "status": p.status,
        "sessionId": p.session_id,
        "rationale": p.rationale,
        "epicTitle": payload.get("epic_title"),
        "epicBody": payload.get("epic_body"),
        "tasks": [{"title": t.get("title"), "body": t.get("body"),
                   "dependsOn": list(t.get("depends_on") or [])}
                  for t in (payload.get("tasks") or []) if isinstance(t, dict)],
        # What a partial apply already created, so a human looking at a blocked
        # initiative sees which issues exist before tapping accept again.
        "created": payload.get("created"),
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "resolvedAt": p.resolved_at.isoformat() if p.resolved_at else None,
    }


async def _latest_materialization(
    db, slug: str,
) -> tuple[Initiative, MaterializationProposal]:
    """The initiative and its newest proposal, whatever its status.

    Newest-of-any-status rather than the pending one: a rejected proposal is
    still what the human last saw, and hiding it would make the page go blank
    between a rejection and the next decompose session's ingest."""
    ini = (await db.execute(select(Initiative).where(
        Initiative.slug == slug))).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    prop = (await db.execute(
        select(MaterializationProposal)
        .where(MaterializationProposal.initiative_id == ini.id)
        .order_by(MaterializationProposal.created_at.desc())
    )).scalars().first()
    if prop is None:
        raise HTTPException(status_code=404, detail={
            "error": "no_proposal",
            "message": f"initiative '{slug}' has no materialization proposal"})
    return ini, prop


def _require_pending(prop: MaterializationProposal) -> None:
    if prop.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_pending",
            "message": f"proposal is '{prop.status}', not pending"})


@router.get("/initiatives/{slug}/materialization")
async def get_materialization(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The initiative's latest decomposition proposal (spec 003 §7b)."""
    _ini, prop = await _latest_materialization(db, slug)
    return _materialization_to_dict(prop)


@router.post("/initiatives/{slug}/materialization/accept")
async def accept_materialization(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The human gate: create the epic, its task sub-issues and the chain.

    Every GitHub call belongs to the orchestrator seam — the router only decides
    that a human said yes. A failure is a 502 with the step that failed and the
    proposal still ``pending``, because the apply is resumable: tapping accept
    again continues from the issues the journal already recorded rather than
    creating a second set.

    Materialize is the plan stage's terminal action (spec §7b), so a confirmed
    accept IS the stage's human gate: the apply advances the initiative and this
    writes the SAME ``sdlc.initiative.advance`` audit row the Advance route
    writes, marked with the reason, so the stage history reads the same either
    way."""
    _ini, prop = await _latest_materialization(db, slug)
    _require_pending(prop)
    result = await request.app.state.sessions.sdlc_materialize_apply(prop.id)
    await record_audit(
        db=db,
        type="sdlc.materialization.accept",
        identity=identity,
        detail={"slug": slug, "proposalId": prop.id, "ok": bool(result.get("ok")),
                "step": result.get("step"), "tasks": result.get("tasks")},
    )
    if not result.get("ok"):
        # Commit the audit BEFORE raising: the dependency rolls the session back
        # on an exception, and a failed materialization is exactly the attempt
        # the trail must keep.
        await db.commit()
        # An apply already running for this proposal is a CONFLICT, not a gateway
        # failure: nothing went wrong, the same write is in flight, and the answer
        # a second tap needs is "wait", not "retry" (C4-R17).
        if result.get("step") == "ownership":
            raise HTTPException(status_code=409, detail=result)
        if result.get("step") == "in_flight":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "apply_in_flight",
                "message": str(result.get("error")
                               or "an apply for this proposal is already running")})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={
            "error": "materialize_failed", "step": result.get("step"),
            "message": str(result.get("error") or "materialization failed")})
    await record_audit(
        db=db,
        type="sdlc.initiative.advance",
        identity=identity,
        detail={"slug": slug, "stage": result.get("stage"), "reason": "materialize"},
    )
    return result


@router.post("/initiatives/{slug}/materialization/reject")
async def reject_materialization(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Turn the decomposition down, and leave the initiative somewhere the loop
    can still act on: the rejection frees the synthesizer to decompose the plan
    again on a later tick (C4-R14/R15), and the rejected row is what moves the
    attempt key so the next tick spawns a NEW session instead of re-reading the
    one just turned down.

    A proposal rejected after a FAILED PARTIAL apply is the exception (C4-R19a).
    Nothing here undoes anything: the epic and any tasks the journal recorded
    still exist on GitHub, so the initiative stays ``blocked`` where the failed
    apply left it — re-decomposing a plan whose epic already exists would create
    a second one. Closing those issues is a human's call, and the proposal's
    ``created`` map (returned by the GET) is the list of what exists.

    With nothing created, ``blocked`` would be a dead end: ``next_action`` answers
    ``NeedsHuman`` for a blocked initiative, so the very rejection meant to invite
    another attempt would prevent one. Back to ``awaiting_human`` — the parked
    state the plan stage already uses — and the next tick decomposes again."""
    _ini, prop = await _latest_materialization(db, slug)
    _require_pending(prop)
    prop.status = "rejected"
    prop.resolved_at = datetime.now(timezone.utc)
    created = (prop.payload or {}).get("created") or {}
    if not (created.get("epic") or created.get("tasks")):
        _ini.status = "awaiting_human"
    await db.flush()
    await record_audit(db=db, type="sdlc.materialization.reject", identity=identity,
                       detail={"slug": slug, "proposalId": prop.id})
    return _materialization_to_dict(prop)


# --------------------------------------------------------------------------- #
# Triage queue — the Maintain loop's human surface (#804)                       #
# --------------------------------------------------------------------------- #

#: What a human may do to a queued incident (design §6, issue #804: "triage now /
#: schedule / dismiss-with-reason"). One vocabulary, shared with the web client.
TRIAGE_ACTIONS: tuple[str, ...] = ("triage_now", "schedule", "dismiss")

#: An incident SDLC Intake has already adopted is an Initiative now — it has a
#: branch, a session and (soon) a PR. Re-dispositioning the queue row would
#: change nothing about any of that while making the queue lie about it, so every
#: action refuses on this status rather than pretending to work.
_ADOPTED = "intake"


class TriageActionBody(BaseModel):
    action: str
    # Only for `schedule`. Timezone-aware or naive; naive is read as UTC, matching
    # every other datetime boundary in this app.
    scheduledFor: datetime | None = None
    # Required for `dismiss`. A dismissal without a reason is indistinguishable
    # from an unexplained one six weeks later, and this queue is an audit trail.
    reason: str | None = None


@router.get("/triage")
async def triage(
    status_filter: str | None = None,
    limit: int = 100,
    _identity: str = Depends(current_identity),
) -> dict:
    """The control-band triage queue, newest first (#804).

    ``status_filter`` is a comma-separated subset of ``TRIAGE_STATUSES``; omit it
    for everything. The web queue asks for everything and groups client-side —
    the queue is bounded by the monitor's own incident ceiling, so paging it
    would add a control the data cannot outgrow.
    """
    statuses = None
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        unknown = [s for s in statuses if s not in TRIAGE_STATUSES]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_status", "message": f"unknown status(es): {unknown}"})
    items = await triage_store.list_items(
        statuses=statuses, limit=max(1, min(int(limit), 500)))
    return {"items": [triage_store.item_to_dict(i) for i in items]}


@router.post("/triage/{item_id}/action")
async def triage_action(
    item_id: str,
    body: TriageActionBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Disposition one incident: triage it now, schedule it, or dismiss it.

    ``triage_now`` queues the incident for SDLC Intake — the same status a 3σ
    breach reaches on its own. That is deliberate: the human tap and the
    automatic path converge on ONE state, so there is a single way an incident
    becomes an ``intent.md`` and no second code path to keep in step.
    """
    if body.action not in TRIAGE_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_action",
                    "message": f"action must be one of {', '.join(TRIAGE_ACTIONS)}"})
    item = (
        await db.execute(select(TriageItem).where(TriageItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if item.status == _ADOPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_adopted",
                    "message": "SDLC Intake has already adopted this incident; "
                               "act on the initiative instead"})

    detail: dict = {"key": item.key, "action": body.action}
    if body.action == "triage_now":
        item.status = "queued"
        item.scheduled_for = None
    elif body.action == "schedule":
        when = body.scheduledFor
        if when is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "missing_scheduled_for",
                        "message": "schedule requires scheduledFor"})
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=422,
                detail={"error": "scheduled_in_the_past",
                        "message": "scheduledFor must be in the future"})
        item.status = "scheduled"
        item.scheduled_for = when
        detail["scheduledFor"] = when.isoformat()
    else:  # dismiss
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(
                status_code=422,
                detail={"error": "missing_reason",
                        "message": "dismiss requires a reason"})
        item.status = "dismissed"
        item.dismissed_reason = reason
        item.scheduled_for = None
        detail["reason"] = reason

    await db.flush()
    await record_audit(db=db, type=f"sdlc.triage.{body.action}", identity=identity,
                       detail=detail)
    return triage_store.item_to_dict(item)
