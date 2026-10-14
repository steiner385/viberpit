"""Roadmap tree endpoints (spec 2026-09-07 §10): the map every product renders
from, and the issue leaves under one node. Reads only; a fixed number of grouped
queries per request, never one per node."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_

from server.api.pm import _queue_inputs
from server.api.sdlc import _capabilities_batch, _descendants, _progress_rolled_up
from server.auth.middleware import current_identity
from server.automations.product_policy import effective_policy, rank_kwargs
from server.db import get_db
from server.db.models import (
    Feature, FeatureLink, GhProjectItem, Initiative, Persona, PersonaFeature, Project, ProjectRepo,
    Session as DbSession, TriageItem, TriageProposal, WorkItem, WorkItemDep)
from server.pm.attention import (WAITING_KEYS, assigned_to_owner, owner_logins,
                                 waiting_reason_for_skip)
from server.pm.desired_state import LIVE_SESSION_STATUSES
from server.pm.drift import is_open_blocker
from server.pm.map_changes import Pointer, open_pointer_rows
from server.pm.mirror_store import blocker_states, blocker_states_stmt
from server.pm.outcomes import (body_fingerprint, bounces_stmt, not_needed_stmt,
                                not_needed_suppression, rows_to_bounces, verdicts_by_ref)
from server.pm.queue import QueueRow, rank
from server.pm.score import rice_score
from server.pm.triage_accept import _aware, awaiting_decision_clause
from server.pm.tree import (
    CapabilityIn, InitiativeIn, ProductIn, build_product_tree, derived_stage, issue_reason,
    issue_tier, needs_you_total)

router = APIRouter(prefix="/api/pm", tags=["pm"])


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


def _threshold_for(project: Project | None) -> int:
    """This product's D8 bounce threshold — the ONE reading both handlers use.

    A missing product (an unowned feature, an initiative with no project) falls
    back through ``effective_policy(None)`` to the SCHEMA's default, never to a
    hand-written literal: the two handlers defaulted to 0 and 2 respectively
    before, so the same orphan leaf could read bounced-out on one surface and
    fine on the other."""
    return int(effective_policy(project.automation_policy if project else None)
               ["give_up_after_bounces"])


def _owners_for(project: Project | None) -> frozenset[str]:
    """This product's owner logins (``attention.owner_logins``) — the ONE reading
    both handlers use for ``issue_tier``'s ``assigned_to_owner``. No product: no
    owner, so nothing is needs-you for being assigned."""
    return owner_logins(effective_policy(project.automation_policy if project else None))


async def _waiting_by_product(db, now: datetime) -> dict[str, dict[str, int]]:
    """Per product key (slug, else project id — ``queue_store``'s keying), the
    waiting counters (design 2026-10-14 §1): every row the queue skips, bucketed
    by ``attention.waiting_reason_for_skip``. THE composition ``GET /api/pm/queue``
    ranks through — ``queue_inputs`` in one sync pass, ``rank`` under
    ``rank_kwargs(policy)`` per product — so the map and the queue cannot
    disagree about why something is not moving. A product without a board has
    no rows and is absent here."""
    rows, policies = await _queue_inputs(db, now)
    by_product: dict[str, list[QueueRow]] = {}
    for r in rows:
        by_product.setdefault(r.product, []).append(r)
    out: dict[str, dict[str, int]] = {}
    for key, prows in by_product.items():
        policy = policies.get(key) or effective_policy(None)
        skipped: list[tuple[QueueRow, str]] = []
        rank(prows, now=now, **rank_kwargs(policy), skipped_out=skipped)
        owners = owner_logins(policy)
        counts = dict.fromkeys(WAITING_KEYS, 0)
        for row, why in skipped:
            bucket = waiting_reason_for_skip(why, row, owners=owners)
            if bucket is not None:
                counts[bucket] += 1
        out[key] = counts
    return out


async def _issue_facts(db, items: list[WorkItem]) -> tuple[
        dict[str, bool], dict[str, str | None], dict[str, list[str]], dict[str, str | None],
        dict[str, int]]:
    """For each work item id: has an open blocker; the live session status if any;
    the open blocker refs; the mirrored board Status if any; the bounce count
    against the CURRENT body (spec §7/§8, D8) — ONE ``bounces_stmt`` execution
    over every item's ``(ref, fingerprint)`` pair, never one query per leaf.
    Blockers fail closed: an unmirrored destination counts as open (spec 003 §10)."""
    if not items:
        return {}, {}, {}, {}, {}
    ids = [w.id for w in items]
    refs = [_ref(w) for w in items]
    deps = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref.in_(refs), WorkItemDep.active.is_(True)))).scalars().all()
    stmt = blocker_states_stmt({d.dst_ref for d in deps if d.dst_ref})
    dst_state = blocker_states((await db.execute(stmt)).all()) if stmt is not None else {}
    blockers: dict[str, list[str]] = {}
    for d in deps:
        if d.src_ref and d.dst_ref and is_open_blocker(dst_state.get(d.dst_ref)):
            blockers.setdefault(d.src_ref, []).append(d.dst_ref)
    sessions = (await db.execute(select(DbSession.origin_ref, DbSession.status).where(
        DbSession.origin_ref.in_(refs), DbSession.status.in_(LIVE_SESSION_STATUSES)))).all()
    session_by_ref = {ref: st for ref, st in sessions}
    board_status: dict[str, str | None] = {}
    if ids:
        rows = (await db.execute(select(GhProjectItem.work_item_id, GhProjectItem.status).where(
            GhProjectItem.work_item_id.in_(ids)).order_by(GhProjectItem.updated_at))).all()
        for wid, st in rows:
            board_status[wid] = st  # last write wins (ordered by updated_at)
    by_id_blocked = {w.id: bool(blockers.get(_ref(w))) for w in items}
    by_id_session = {w.id: session_by_ref.get(_ref(w)) for w in items}
    by_id_blockers = {w.id: blockers.get(_ref(w), []) for w in items}
    by_id_board_status = {w.id: board_status.get(w.id) for w in items}
    pairs = {_ref(w): body_fingerprint(w.title, w.body) for w in items}
    bounce_rows = (await db.execute(bounces_stmt(pairs.items()))).all()
    bounces_by_ref = rows_to_bounces(bounce_rows, pairs.items())
    by_id_bounces = {w.id: bounces_by_ref.get(_ref(w), 0) for w in items}
    return by_id_blocked, by_id_session, by_id_blockers, by_id_board_status, by_id_bounces


def needs_home_stmt(repos: list[str]):
    """The roadmap's unassigned owner-held items, shared with session triage."""
    return select(WorkItem).where(
        WorkItem.repo.in_(repos), WorkItem.state == "open",
        WorkItem.hold_kind == "owner_action", WorkItem.feature_id.is_(None))


@router.get("/map")
async def get_map(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    return {"products": await product_nodes(db),
            "generatedAt": datetime.now(timezone.utc).isoformat()}


async def product_attention_counts(db) -> dict[str, dict]:
    """Per product slug, ``{"needsYou": n, "waiting": {key: n}}`` — exactly what the
    Products map shows (its badge population, ``tree.needs_you_total``, and its
    waiting counters), from the SAME ``product_nodes`` composition ``/map`` serves.
    The needs-you hygiene loop samples it (design 2026-10-14 §2 "Metric"), so the
    trend and the badge cannot disagree."""
    return {node["slug"]: {"needsYou": needs_you_total(node),
                           "waiting": dict(node["counts"]["waiting"])}
            for node in await product_nodes(db)}


async def product_nodes(db) -> list[dict]:
    """Every product's map tree (``tree.build_product_tree``) — the body of ``/map``."""
    projects = (await db.execute(select(Project).order_by(Project.created_at))).scalars().all()
    if not projects:
        return []
    pids = [p.id for p in projects]
    slug_of = {p.id: p.slug for p in projects}

    features = (await db.execute(select(Feature).where(
        Feature.project_id.in_(pids), Feature.orphaned.is_not(True)))).scalars().all()
    feat_by_id = {f.id: f for f in features}
    # Guard every `in_` on a possibly-empty list: SQLAlchemy warns on `in_([])`.
    links = (await db.execute(select(FeatureLink).where(
        FeatureLink.src_feature_id.in_(list(feat_by_id))))).scalars().all() if feat_by_id else []
    # Link destinations may be orphaned or in a project outside `pids`; load them too.
    dst_ids = {link.dst_feature_id for link in links} - set(feat_by_id)
    dst_extra = {f.id: f for f in (await db.execute(select(Feature).where(
        Feature.id.in_(dst_ids)))).scalars().all()} if dst_ids else {}
    dst_projects = {}
    extra_pids = {f.project_id for f in dst_extra.values()} - set(pids)
    if extra_pids:
        dst_projects = {p.id: p.slug for p in (await db.execute(select(Project).where(
            Project.id.in_(extra_pids)))).scalars().all()}
    slug_of.update(dst_projects)
    links_by_src: dict[str, list[tuple[str, str, str]]] = {}
    for link in links:
        dst = feat_by_id.get(link.dst_feature_id) or dst_extra.get(link.dst_feature_id)
        if dst is None:
            continue
        links_by_src.setdefault(link.src_feature_id, []).append(
            (link.kind or "", slug_of.get(dst.project_id) or "", dst.slug or ""))

    # Resolved persona links (#2164/#2453) — same batching shape as links_by_src
    # just above: one query scoped to this request's features, live personas
    # only (an orphaned Persona's leftover PersonaFeature row must never surface
    # in the filter — steiner385/viberpit#2446).
    persona_rows = (await db.execute(select(PersonaFeature.feature_id, Persona.slug).join(
        Persona, PersonaFeature.persona_id == Persona.id).where(
        PersonaFeature.feature_id.in_(list(feat_by_id)),
        Persona.orphaned.isnot(True)))).all() if feat_by_id else []
    personas_by_feature: dict[str, list[str]] = {}
    for fid, pslug in persona_rows:
        if pslug:
            personas_by_feature.setdefault(fid, []).append(pslug)

    open_items = (await db.execute(select(WorkItem).where(
        WorkItem.feature_id.in_(list(feat_by_id)), WorkItem.state == "open",
        WorkItem.kind == "issue"))).scalars().all() \
        if feat_by_id else []
    blocked, session, _, board_status, bounces = await _issue_facts(db, open_items)
    # Threshold per product (D8): a feature belongs to exactly one project, so its
    # issues bounce against THAT product's `give_up_after_bounces`, never a global one.
    threshold_by_project = {p.id: _threshold_for(p) for p in projects}
    default_threshold = _threshold_for(None)   # once, not per item
    # Owners per product, likewise: an issue assigned to ITS product's owner needs you.
    owners_by_project = {p.id: _owners_for(p) for p in projects}
    tiers_by_feature: dict[str, list[str]] = {}
    for w in open_items:
        feat = feat_by_id.get(w.feature_id)
        threshold = (threshold_by_project.get(feat.project_id, default_threshold)
                    if feat else default_threshold)
        owners = owners_by_project.get(feat.project_id, frozenset()) if feat else frozenset()
        reason = issue_reason(state=w.state, bounces=bounces[w.id],
                              give_up_after_bounces=threshold)
        tiers_by_feature.setdefault(w.feature_id, []).append(
            issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id],
                      board_status[w.id], reason=reason, hold_kind=w.hold_kind,
                      assigned_to_owner=assigned_to_owner(w.assignees, owners)))

    initiatives = (await db.execute(select(Initiative).where(
        Initiative.project_id.in_(pids)).order_by(Initiative.created_at))).scalars().all()
    epics = await _progress_rolled_up(db, initiatives, include_children=False)
    caps = await _capabilities_batch(db, initiatives)
    slug_by_id = {i.id: i.slug for i in initiatives}
    phases_by_parent: dict[str, list[Initiative]] = {}
    for i in initiatives:
        if i.parent_initiative_id:
            phases_by_parent.setdefault(i.parent_initiative_id, []).append(i)

    # The open map-changes pointer per product (spec §9) — one batched query
    # through the same lookup the service and the sync loop use (R8b-5).
    map_changes = {pid: Pointer.from_row(row).to_dict()
                   for pid, row in (await open_pointer_rows(db, pids)).items()}

    repos = (await db.execute(select(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))).scalars().all()
    repos_by_project: dict[str, list[str]] = {}
    for r in repos:
        if r.repo:
            repos_by_project.setdefault(r.project_id, []).append(r.repo)
    all_repos = [r for rs in repos_by_project.values() for r in rs]
    pending_by_repo: dict[str, int] = {}
    needs_home_by_repo: dict[str, int] = {}
    incidents_by_repo: dict[str, list[str]] = {}
    if all_repos:
        pending = (await db.execute(select(WorkItem.repo).join(
            TriageProposal, TriageProposal.work_item_id == WorkItem.id).where(
            WorkItem.repo.in_(all_repos), awaiting_decision_clause()))).all()
        for (repo,) in pending:
            pending_by_repo[repo] = pending_by_repo.get(repo, 0) + 1
        needs_home = (await db.execute(needs_home_stmt(all_repos).with_only_columns(WorkItem.repo))).all()
        for (repo,) in needs_home:
            needs_home_by_repo[repo] = needs_home_by_repo.get(repo, 0) + 1
        incidents = (await db.execute(select(TriageItem.repo, TriageItem.status).where(
            TriageItem.status.in_(("open", "scheduled", "diagnosing", "queued")),
            TriageItem.repo.in_(all_repos)))).all()
        for repo, st in incidents:
            incidents_by_repo.setdefault(repo, []).append(st or "")

    waiting_by_product = await _waiting_by_product(db, datetime.now(timezone.utc))

    products = []
    for p in projects:
        my_feats = [f for f in features if f.project_id == p.id]
        slug_by_fid = {f.id: f.slug for f in my_feats}
        cap_inputs = tuple(CapabilityIn(
            id=f.id, slug=f.slug or "", title=f.title, state=f.state, summary=f.summary,
            doc_path=f.repo_path, commit_sha=f.commit_sha, pr_number=f.pr_number,
            parent_slug=slug_by_fid.get(f.parent_feature_id) if f.parent_feature_id else None,
            issue_tiers=tuple(tiers_by_feature.get(f.id, [])),
            links=tuple(links_by_src.get(f.id, [])),
            persona_slugs=tuple(personas_by_feature.get(f.id, [])),
        ) for f in my_feats)
        ini_inputs = []
        for i in initiatives:
            if i.project_id != p.id:
                continue
            cap_list, primary = caps[i.id]
            phases = phases_by_parent.get(i.id, [])
            # None (no phase carries a recognized stage) keeps the row's own
            # stage rather than a phantom value.
            stage = (derived_stage([ph.stage for ph in phases]) or i.stage) if phases else i.stage
            ini_inputs.append(InitiativeIn(
                slug=i.slug or "", title=i.title, stage=stage, status=i.status,
                primary_slug=primary["slug"] if primary else None,
                capability_slugs=tuple(c["slug"] for c in cap_list),
                epic=epics.get(i.id),
                parent_slug=slug_by_id.get(i.parent_initiative_id) if i.parent_initiative_id else None,
                last_auto_gate=i.last_auto_gate,
                # Through `_aware`, like every other datetime this payload
                # carries: the column is DateTime(timezone=True), which SQLite
                # hands back tzinfo-less, so a bare isoformat() would emit an
                # offset on Postgres and none in the tests that pin the shape.
                updated_at=_aware(i.updated_at)))
        my_repos = repos_by_project.get(p.id, [])
        products.append(build_product_tree(ProductIn(
            slug=p.slug or "", name=p.name,
            capabilities=cap_inputs, initiatives=tuple(ini_inputs),
            awaiting_triage=sum(pending_by_repo.get(r, 0) for r in my_repos),
            needs_home=sum(needs_home_by_repo.get(r, 0) for r in my_repos),
            incident_statuses=tuple(s for r in my_repos for s in incidents_by_repo.get(r, [])),
            map_changes=map_changes.get(p.id),
            waiting=waiting_by_product.get(p.slug or p.id, {}),
        )))
    return products


async def _not_needed_until(db, items: list[WorkItem], now: datetime) -> dict[str, datetime]:
    """``WorkItem.id`` → when its not-needed suppression ends, for every OPEN item the
    queue currently skips as ``not_needed`` (#2683 review): the same verdict rows and
    the same rule (``outcomes.not_needed_suppression``) the queue loader applies, so
    the Roadmap can say "session judged no PR needed — re-check <date>" instead of
    the item silently never being picked."""
    open_items = [w for w in items if w.state == "open"]
    if not open_items:
        return {}
    verdicts = verdicts_by_ref((await db.execute(
        not_needed_stmt([_ref(w) for w in open_items]))).all())
    out: dict[str, datetime] = {}
    for w in open_items:
        if _ref(w) not in verdicts:
            continue
        until, _ = not_needed_suppression(
            verdicts[_ref(w)], title=w.title, body=w.body, labels=w.labels,
            last_human_comment_at=w.last_human_comment_at,
            last_reopened_at=w.last_reopened_at)
        if until is not None and now < until:
            out[w.id] = until
    return out


def _leaf(w: WorkItem, tier: str, blockers: list[str], session_status: str | None,
          parent_ref: str | None, *, reason: str | None = None, bounces: int = 0,
          not_needed_until: datetime | None = None) -> dict:
    score = rice_score(w.reach, w.impact, w.effort, w.confidence)
    held_at = _aware(w.hold_set_at)
    return {
        "id": w.id, "ref": _ref(w), "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "url": w.url, "state": w.state, "triageState": w.triage_state,
        "score": score, "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence, "tier": tier, "blockedBy": blockers,
        "parentRef": parent_ref, "featureId": w.feature_id, "sessionStatus": session_status,
        "ghCreatedAt": w.gh_created_at.isoformat() if w.gh_created_at else None,
        "reason": reason, "bounces": bounces,
        "holdKind": w.hold_kind, "holdReason": w.hold_reason, "holdBy": w.hold_by,
        "holdSetAt": held_at.isoformat() if held_at else None,
        # #2683: a session judged no PR needed and the queue skips the issue until
        # this moment (or until it materially changes). None when not suppressed.
        "notNeededUntil": not_needed_until.isoformat() if not_needed_until else None,
    }


def _leaf_sort_key(leaf: dict) -> tuple:
    return (
        0 if leaf["state"] == "open" else 1,
        0 if leaf["score"] is not None else 1,
        -(leaf["score"] or 0.0),
        leaf["ghCreatedAt"] or "9999",
        leaf["number"] or 0,
    )


@router.get("/leaves")
async def get_leaves(
    capability: str | None = Query(default=None),
    initiative: str | None = Query(default=None),
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if bool(capability) == bool(initiative):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
            "error": "bad_scope", "message": "pass exactly one of capability= or initiative="})
    if capability:
        feat = (await db.execute(select(Feature).where(Feature.id == capability))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        project_id = feat.project_id
        items = (await db.execute(select(WorkItem).where(WorkItem.feature_id == feat.id))).scalars().all()
    else:
        ini = (await db.execute(select(Initiative).where(Initiative.slug == initiative))).scalar_one_or_none()
        if ini is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        project_id = ini.project_id
        items = []
        descendants = await _descendants(db, [ini])
        if descendants:
            # A program (an initiative with phases): its epic's direct sub-issues
            # are sub-epics, not leaves, which would contradict epic.total (the
            # leaf roll-up, tree.rollup_epic_progress). List the direct sub-issues
            # of every LEAF descendant's epic instead, so the two agree.
            parent_ids = {d.parent_initiative_id for d in descendants if d.parent_initiative_id}
            leaf_inits = [d for d in descendants if d.id not in parent_ids]
            pointers = [(leaf.epic_repo, leaf.epic_number) for leaf in leaf_inits
                       if leaf.epic_repo and leaf.epic_number]
            epic_ids: list[str] = []
            if pointers:
                epic_ids = [w.id for w in (await db.execute(select(WorkItem).where(
                    WorkItem.kind == "issue",
                    tuple_(WorkItem.repo, WorkItem.number).in_(pointers)))).scalars().all()]
            if epic_ids:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.parent_work_item_id.in_(epic_ids)))).scalars().all()
        elif ini.epic_repo and ini.epic_number:
            epic = (await db.execute(select(WorkItem).where(
                WorkItem.repo == ini.epic_repo, WorkItem.number == ini.epic_number,
                WorkItem.kind == "issue"))).scalar_one_or_none()
            if epic is not None:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.parent_work_item_id == epic.id))).scalars().all()
    blocked, session, blockers, board_status, bounces = await _issue_facts(db, items)
    project = (await db.execute(select(Project).where(
        Project.id == project_id))).scalar_one_or_none() if project_id else None
    threshold = _threshold_for(project)
    owners = _owners_for(project)
    parent_ids = {w.parent_work_item_id for w in items if w.parent_work_item_id}
    parents = {p.id: _ref(p) for p in (await db.execute(select(WorkItem).where(
        WorkItem.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}
    suppressed = await _not_needed_until(db, list(items), datetime.now(timezone.utc))
    leaves = []
    for w in items:
        reason = issue_reason(state=w.state, bounces=bounces[w.id],
                              give_up_after_bounces=threshold)
        leaves.append(_leaf(
            w, issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id],
                         board_status[w.id], reason=reason, hold_kind=w.hold_kind,
                         assigned_to_owner=assigned_to_owner(w.assignees, owners)),
            blockers[w.id], session[w.id], parents.get(w.parent_work_item_id),
            reason=reason, bounces=bounces[w.id],
            not_needed_until=suppressed.get(w.id)))
    leaves.sort(key=_leaf_sort_key)
    return {"items": leaves}
