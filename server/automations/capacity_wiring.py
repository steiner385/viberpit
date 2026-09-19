"""Wire the capacity readout's injected seams onto the real orchestrator/DB
(spec 2026-09-19 §5). Pure plumbing — no policy lives here, only where each
number comes from."""
from __future__ import annotations

from sqlalchemy import select

from server.api.accounts import account_headroom
from server.api.fleet import host_to_wire
from server.automations.capacity_store import read_plan
from server.automations.product_admission import gather_implementation_snapshot
from server.config import get_settings
from server.db import db_session
from server.db.models import Host, Project, ProjectRepo, Session as DbSession

from .capacity_readout import CapacitySources


async def _off() -> tuple[str, dict]:
    return "off", {}


def sources_for(orch) -> CapacitySources:
    async def hosts() -> list[dict]:
        async with db_session() as db:
            rows = (await db.execute(select(Host).order_by(Host.name))).scalars().all()
        return [host_to_wire(r) for r in rows]

    async def products() -> list[dict]:
        async with db_session() as db:
            rows = (await db.execute(
                select(Project, ProjectRepo)
                .join(ProjectRepo, ProjectRepo.project_id == Project.id)
                .where(ProjectRepo.is_primary.is_(True))
            )).all()
        return [{
            "slug": project.slug, "name": project.name,
            "priorityRank": project.priority_rank,
            "createdAt": project.created_at.isoformat() if project.created_at else None,
            "policy": project.automation_policy, "primaryRepo": repo.repo,
        } for project, repo in rows if project.slug]

    async def decide(repo: str, slug: str):
        snap = await gather_implementation_snapshot(
            [repo], requested_product=slug, context=orch._backlog_ctx)
        return snap.decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")

    async def session_status(sid: str) -> str | None:
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession.status).where(DbSession.id == sid))).first()
        return None if row is None else row[0]

    return CapacitySources(
        global_cap=orch._max_active_sessions,
        global_active=orch.active_session_count,
        hosts=hosts,
        accounts=lambda reserve: account_headroom(orch, reserve_pct=reserve),
        runner_config=orch._backlog_read_config,
        target_states=orch._backlog_ctx.list_target_states,
        candidates=orch._backlog_count_candidates,
        products=products,
        decide=decide,
        session_status=session_status,
        plan=read_plan,
        # Task 7 supplies both of these for real: a governor config store/read
        # seam and the kill-switch setting. Until then the governor reads as
        # permanently "off" and the kill switch defaults on.
        governor_config=getattr(orch, "_capacity_governor_read_config", None) or _off,
        kill_switch=lambda: bool(getattr(get_settings(), "capacity_governor_enabled", True)),
    )
