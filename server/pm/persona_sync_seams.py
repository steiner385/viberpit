"""The persona-index DB-write seam (issue #2164) — mirrors
``server/pm/feature_sync_seams.py``'s ``list_feature_rows``/``apply_feature_plan``
but with no clone-dependent read half: personas are default-branch-only, so
there is exactly one seam pair, used identically by the daemon's pm-sync tick."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Persona

from .persona_index import PersonaIndexPlan, PersonaRowView


async def list_persona_rows(project_id: str) -> list[PersonaRowView]:
    async with db_session() as db:
        rows = (await db.execute(
            select(Persona).where(Persona.project_id == project_id)
        )).scalars().all()
    return [PersonaRowView(slug=p.slug or "", orphaned=bool(p.orphaned)) for p in rows]


async def apply_persona_plan(project_id: str, plan: PersonaIndexPlan) -> None:
    async with db_session() as db:
        for up in plan.upserts:
            row = (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.slug == up.slug)
            )).scalar_one_or_none()
            if row is None:
                row = Persona(project_id=project_id, slug=up.slug)
                db.add(row)
            row.title = up.title
            row.role = up.role
            row.age_band = up.age_band
            row.tier = up.tier
            row.summary = up.summary
            row.repo_path = up.repo_path
            row.orphaned = False
        for slug in plan.orphan_slugs:
            row = (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.slug == slug)
            )).scalar_one_or_none()
            if row is not None:
                row.orphaned = True
        await db.flush()
