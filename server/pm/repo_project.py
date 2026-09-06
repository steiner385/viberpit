"""THE answer to "which project owns this repo?" (constitution VI).

Both the PM API router (``server/api/pm.py``) and the daemon-side pm-triage
seams (``server/sessions/orchestrator.py``) need repo→project ownership, and
neither may import the other — a router imported into the orchestrator is a
layering inversion, which is exactly how this concept came to be implemented
twice. It lives here, in the layer both already depend on, so the tie-break
cannot silently diverge between the inbox a human reads and the loop that acts.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import Project, ProjectRepo


@dataclass(frozen=True)
class RepoOwner:
    """The project that owns a repo. ``project_id`` scopes per-project-unique
    lookups (``Feature.slug``); ``slug`` names the project for the seams that are
    keyed by slug."""

    project_id: str
    slug: str


async def owning_projects_by_repo(
    db: AsyncSession, repos: Collection[str],
) -> dict[str, RepoOwner]:
    """``repo`` → :class:`RepoOwner` for each of *repos* that a project owns.

    ONE query for the whole batch — callers resolve a classify batch, a drift
    cluster set or a triage page at once, so this must never become a per-repo
    round trip. A repo absent from the result is owned by no project, and every
    caller treats that as *skip, do not guess*: the phase-2 seams are keyed by
    project slug and ``Feature.slug`` is unique only within a project, so
    guessing binds items to another product's rows.

    **Ownership is ambiguous by construction.** ``ProjectRepo``'s uniqueness is
    ``(project_id, repo)`` and ``POST /api/pm/projects`` does no cross-project
    check, so one repo CAN legally be registered under several projects. This
    resolves that by lowest ``Project.slug``, which is *deterministic*, not
    *correct* — it only guarantees the winner cannot flip between requests or
    ticks (an unordered dict build picked an arbitrary row). The real fix is a
    uniqueness rule at registration time; until then, a repo under two projects
    is a data-entry bug this function hides rather than solves.

    Projects with no ``slug`` are skipped: the writer-side invariant is that
    ``Project.slug`` is always set (it is nullable only for reconcile), an
    unnameable project is useless to the slug-keyed callers, and ordering by a
    NULL slug is dialect-dependent (SQLite sorts NULLs first, Postgres last) —
    i.e. the one input that would make the tie-break non-deterministic.
    """
    wanted = {r for r in repos if r}
    if not wanted:
        return {}
    rows = (await db.execute(
        select(ProjectRepo.repo, Project.id, Project.slug)
        .join(Project, Project.id == ProjectRepo.project_id)
        .where(ProjectRepo.repo.in_(wanted), Project.slug.isnot(None))
        .order_by(Project.slug))).all()
    out: dict[str, RepoOwner] = {}
    for repo, project_id, slug in rows:
        if repo and repo not in out:  # first row wins → lowest slug
            out[repo] = RepoOwner(project_id=project_id, slug=slug)
    return out
