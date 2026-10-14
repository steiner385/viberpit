"""Stale dependency-edge pruning — THE core behind ``POST /api/pm/projects/{slug}/deps/prune``
(and the assistant's tool, which calls that route) and the needs-you hygiene loop's
``resolved_edges`` rule (design 2026-10-14 §2), so a person's prune and the machine's
cannot disagree about which edge may go or how.

``dep_scope`` is the one load of a product's dependency rows with every blocker's state
resolved (shared with the deps view and the stale preview); ``stale_edges`` classifies
it (``stale_deps.classify_stale``, pure); ``prune`` re-classifies each requested pair
against the mirror NOW and removes only what is still prunable.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select

from server.audit import record_audit
from server.db.models import Project, ProjectRepo, WorkItem, WorkItemDep

from .drift import DepEdgeView
from .mirror_store import blocker_states, blocker_states_stmt, deps_in_repos
from .refs import ISSUE_REF_RE
from .stale_deps import StaleEdge, classify_stale

#: ``(work_item_id, blocker_ref) -> {"ok", "step", "error"}`` — the orchestrator's
#: ``pm_remove_dependency`` seam (its GitHub ladder and journal).
RemoveDependency = Callable[[str, str], Awaitable[dict]]


@dataclass
class DepScope:
    """One product's dependency rows with every blocker's state resolved — the shared
    load behind the deps view and the stale-edge preview/prune, so they can never
    disagree about which edge exists or whether its blocker is resolved."""
    items: dict[str, WorkItem]
    open_refs: set[str]
    edges: list[DepEdgeView]
    cross_states: dict[str, str]

    def dst_state(self, ref: str) -> str | None:
        if ref in self.items:
            return self.items[ref].state
        return self.cross_states.get(ref)


async def dep_scope(db, p: Project) -> DepScope:
    repos = {r.repo for r in (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id)
    )).scalars().all() if r.repo}
    work_items_raw = (await db.execute(
        select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "issue")
    )).scalars().all()
    items: dict[str, WorkItem] = {f"{w.repo}#{w.number}": w for w in work_items_raw}
    open_refs = {ref for ref, w in items.items() if (w.state or "open") == "open"}
    # Push the src_ref repo-prefix filter into SQL (issue #973) — keep only deps
    # whose src belongs to a project repo, instead of scanning the whole
    # WorkItemDep table in Python. Shared with the reconciler's facts loader.
    dep_rows = list((await db.execute(
        select(WorkItemDep).where(deps_in_repos(repos)))).scalars().all())
    # Cross-product visibility (spec goal): a dep may target another onboarded
    # product's mirrored issue, or a pull request (#2979). Resolve dst refs that are
    # NOT in the project-scoped issue map through the queue's own blocker lookup
    # (single query), so their dstState is populated and they don't land in
    # unresolvedTargets. Genuinely unmirrored targets stay null → unresolvedTargets.
    stmt = blocker_states_stmt(
        {d.dst_ref for d in dep_rows if d.dst_ref and d.dst_ref not in items})
    cross_states: dict[str, str] = (
        blocker_states((await db.execute(stmt)).all()) if stmt is not None else {})
    scope = DepScope(items=items, open_refs=open_refs, edges=[], cross_states=cross_states)
    scope.edges = [DepEdgeView(
        src_ref=d.src_ref or "",
        dst_ref=d.dst_ref or "",
        source=d.source or "",
        active=bool(d.active),
        dst_state=scope.dst_state(d.dst_ref or ""),
    ) for d in dep_rows]
    return scope


def stale_edges(scope: DepScope) -> list[StaleEdge]:
    return classify_stale(scope.edges, open_srcs=scope.open_refs,
                          bodies={ref: scope.items[ref].body for ref in scope.open_refs})


async def prune(db, *, slug: str, scope: DepScope, pairs: Iterable[tuple[str, str]],
                remove_dependency: RemoveDependency, identity: str,
                via: str | None = None) -> list[dict]:
    """Prune each ``(src, blocker)`` pair that is STILL a prunable stale edge of
    *scope*, one result per distinct pair, each audited ``pm.workitem.dep_prune``.

    Re-classified against *scope* (loaded now), so an edge that stopped being stale
    since the caller looked (the blocker reopened, the text came back) is refused,
    never removed. ``native_resolved`` goes through *remove_dependency* (the same
    GitHub seam as the roadmap's Remove); ``prose_vanished`` is deactivated locally
    (the text it was derived from is gone, so no sync re-creates it);
    ``derived_resolved`` is refused — a sync would re-derive it, so the fix is
    editing the issue body. A partial success is reported as one."""
    stale = {(s.src, s.blocker): s for s in stale_edges(scope)}
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw_src, raw_blocker in pairs:
        pair = (raw_src.strip(), raw_blocker.strip())
        if pair in seen:
            continue
        seen.add(pair)
        src, blocker = pair
        edge = stale.get(pair)
        pruned: str | None = None
        refused: str | None = None
        message: str | None = None
        step: str | None = None
        if not ISSUE_REF_RE.match(src) or not ISSUE_REF_RE.match(blocker):
            refused, message = "bad_ref", "src and blocker must be owner/repo#N"
        elif edge is None:
            refused = "not_stale"
            message = (f"{src} ← {blocker} is not a stale active edge of {slug} any more "
                       "(its blocker is open or unknown, or the edge is gone)")
        elif not edge.prunable:
            refused = "rederived"
            message = (f"{src} still names {blocker} in its body (or epic order), so the "
                       "next sync would re-create the edge; edit the issue instead")
        elif edge.kind == "native_resolved":
            result = await remove_dependency(scope.items[src].id, blocker)
            step = result.get("step")
            if result.get("ok"):
                pruned = "native"
            else:
                refused = "github"
                message = str(result.get("error") or "dependency removal failed")
        else:
            rows = (await db.execute(select(WorkItemDep).where(
                WorkItemDep.src_ref == src, WorkItemDep.dst_ref == blocker,
                WorkItemDep.source == "prose", WorkItemDep.active.is_(True)))).scalars().all()
            for row in rows:
                row.active = False
            await db.flush()
            pruned = "prose"
        await record_audit(db=db, type="pm.workitem.dep_prune", identity=identity, via=via,
                           detail={"slug": slug, "src": src, "blocker": blocker,
                                   "kind": edge.kind if edge else None,
                                   "ok": pruned is not None, "refused": refused, "step": step})
        results.append({"src": src, "blocker": blocker, "pruned": pruned,
                        "refused": refused, "message": message})
    return results
