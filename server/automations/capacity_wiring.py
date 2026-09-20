"""Wire the capacity readout's injected seams onto the real orchestrator/DB
(spec 2026-09-19 §5). Pure plumbing — no policy lives here, only where each
number comes from."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import and_, select

from server.api.accounts import account_headroom
from server.api.fleet import host_to_wire
from server.automations.capacity_store import read_plan
from server.automations.product_admission import gather_implementation_snapshot
from server.config import get_settings
from server.db import db_session
from server.db.models import Host, Project, ProjectRepo, Session as DbSession
from server.sessions.global_cap import read_global_cap

from .capacity_readout import CapacitySources

if TYPE_CHECKING:
    from server.automations.product_admission import ImplementationSnapshot

log = logging.getLogger(__name__)


async def _safe_plan() -> dict | None:
    """The readout's ``plan`` seam: the RAW plan (not ``live_plan()`` — the
    readout computes ``planLive`` itself from this same raw value, so this
    must not pre-filter it), but never letting a DB blip 500 ``GET
    /api/capacity`` (deferred-now 1, fix round 2)."""
    try:
        return await read_plan()
    except Exception:
        log.warning("capacity_wiring.plan_read_failed", exc_info=True)
        return None


def sources_for(orch) -> CapacitySources:
    async def hosts() -> list[dict]:
        async with db_session() as db:
            rows = (await db.execute(select(Host).order_by(Host.name))).scalars().all()
        return [host_to_wire(r) for r in rows]

    async def products() -> list[dict]:
        # OUTER join: a product with no primary repo (or none yet flagged primary)
        # must still appear in the readout, not silently vanish (spec gap c).
        async with db_session() as db:
            rows = (await db.execute(
                select(Project, ProjectRepo)
                .outerjoin(ProjectRepo, and_(ProjectRepo.project_id == Project.id,
                                             ProjectRepo.is_primary.is_(True)))
            )).all()
        seen: set[str] = set()
        out: list[dict] = []
        for project, repo in rows:
            # Defensive dedupe: the join condition already scopes to is_primary, so
            # this only fires if stored data ever carries >1 primary repo per project.
            if not project.slug or project.id in seen:
                continue
            seen.add(project.id)
            out.append({
                "slug": project.slug, "name": project.name,
                "priorityRank": project.priority_rank,
                "createdAt": project.created_at.isoformat() if project.created_at else None,
                "policy": project.automation_policy,
                "primaryRepo": repo.repo if repo is not None else None,
            })
        return out

    # ONE gather_implementation_snapshot() per readout tick, not one per product:
    # it shells out to `gh` per repo, so calling it product-at-a-time here would
    # spawn P×O `gh` subprocesses every time a readout is built — the governor
    # loop ticks every 60s, and GET /api/capacity is also polled directly by the
    # Delivery → Capacity page every 30s — instead of a single batched call.
    # Lazily built on the first `decide()` of a tick and
    # reused for the rest of that tick; `sources_for` is called fresh per readout,
    # so a new snapshot is built next tick too — this is a per-tick cache, not a
    # long-lived one.
    _snapshot: "ImplementationSnapshot | None" = None

    async def decide(repo: str, slug: str):
        nonlocal _snapshot
        if _snapshot is None:
            all_products = await products()
            repos = sorted({p["primaryRepo"] for p in all_products if p.get("primaryRepo")})
            _snapshot = await gather_implementation_snapshot(repos, context=orch._backlog_ctx)
        return _snapshot.decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")

    async def session_status(sid: str) -> str | None:
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession.status).where(DbSession.id == sid))).first()
        return None if row is None else row[0]

    async def global_limits() -> dict | None:
        """The Global row's derivation (spec 2026-09-20 D3). Same contract as
        ``_safe_plan``: a DB blip degrades the three fields to null instead of
        500ing ``GET /api/capacity`` or breaking the governor's tick."""
        try:
            async with db_session() as db:
                return (await read_global_cap(db)).to_wire()
        except Exception:
            log.warning("capacity_wiring.global_limits_read_failed", exc_info=True)
            return None

    return CapacitySources(
        # `global_cap` is the same effective value the admission gate compares
        # against (orchestrator._max_active_sessions → global_cap.read_global_cap);
        # `global_limits` is that value's derivation for the Global row.
        global_cap=orch._max_active_sessions,
        global_limits=global_limits,
        # NOT active_session_count (the deploy-drain counter, which ignores
        # idle/done records) — this must be the same counter the global-cap
        # admission gate itself compares against (the three
        # `_active_session_count()` sites in orchestrator.py),
        # or the readout's "global.free" lies relative to what actually blocks a spawn.
        global_active=orch._active_session_count,
        hosts=hosts,
        accounts=lambda reserve: account_headroom(orch, reserve_pct=reserve),
        # apply_plan=False: the governor derives its next plan from the
        # OPERATOR's stored ceiling, never from a prior tick's own overlaid
        # output — feeding that back in would ratchet fleetConcurrency down
        # every tick with no way back up (F1, fix round 1).
        runner_config=lambda: orch._backlog_read_config(apply_plan=False),
        target_states=orch._backlog_ctx.list_target_states,
        candidates=orch._backlog_count_candidates,
        products=products,
        decide=decide,
        session_status=session_status,
        plan=_safe_plan,
        governor_config=orch._capacity_governor_read_config,
        kill_switch=lambda: bool(get_settings().capacity_governor_enabled),
    )
