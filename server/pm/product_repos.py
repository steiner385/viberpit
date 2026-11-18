"""Single source of truth for which repos of a product are live (not retired).

Every queue / sync / clone / filing path asks here instead of reading
``ProjectRepo`` directly. ``is_retired`` (async, no session argument) answers from a
30 s process snapshot of the retired set and refills it itself when missing or expired;
a DB error is fail-open ("not retired").
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.db.models import ProjectRepo

log = logging.getLogger(__name__)

# SQL clause: a repo that has not been retired.
ACTIVE = ProjectRepo.retired_at.is_(None)

# ORDER BY for a product's repos: primary first, then active, then retired, then oldest.
REPO_ORDER = (ProjectRepo.is_primary.desc().nulls_last(), ProjectRepo.retired_at.is_not(None),
              ProjectRepo.created_at, ProjectRepo.repo)

RETIRED_CACHE_TTL_S = 30.0

_lock = threading.Lock()
_retired_cache: tuple[float, frozenset[str]] | None = None


class RepoRetired(ValueError):
    """A retired repo was used where only an active one is allowed. The app maps it
    to HTTP 409 ``repo_retired``; ``error_data`` rides the daemon RPC envelope so a
    remote edge rebuilds it (``server.ipc.client.reconstruct_create_session_error``)."""

    def __init__(self, repo: str, superseded_by: str | None = None):
        self.repo = repo
        self.superseded_by = superseded_by
        self.error_data = {"repo": repo, "supersededBy": superseded_by}
        super().__init__(f"{repo} is retired" + (f"; use {superseded_by}" if superseded_by else ""))


def _store(repos: frozenset[str]) -> frozenset[str]:
    global _retired_cache
    with _lock:
        _retired_cache = (time.monotonic(), repos)
    return repos


def invalidate_retired_cache() -> None:
    global _retired_cache
    with _lock:
        _retired_cache = None


async def is_retired(repo: str) -> bool:
    """Whether ``repo`` ("owner/name", case-insensitive) is retired. Serves from a
    process snapshot younger than ``RETIRED_CACHE_TTL_S``; a missing/expired one is
    refilled from the DB here, so a process that never primed it (the daemon) still
    answers correctly. A DB failure is fail-open (False), logged at INFO."""
    with _lock:
        cached = _retired_cache
    if cached is None or time.monotonic() - cached[0] > RETIRED_CACHE_TTL_S:
        from server.db import db_session
        try:
            async with db_session() as db:
                repos = await retired_repo_set(db)
        except Exception as exc:  # noqa: BLE001 - fail-open by design
            log.info("retired-repo snapshot refresh failed; treating as not retired: %s", exc)
            return False
    else:
        repos = cached[1]
    return (repo or "").casefold() in repos


async def require_active(repo: str | None) -> None:
    """THE refusal for new work against a retired repo (sessions, issue/comment
    writes): raises :class:`RepoRetired` naming its ``superseded_by``. Cheap for an
    active repo (the ``is_retired`` snapshot); only a retired one costs a DB read."""
    if not repo or not await is_retired(repo):
        return
    superseded_by = None
    from server.db import db_session
    try:
        async with db_session() as db:
            superseded_by = (await db.execute(
                select(ProjectRepo.superseded_by).where(
                    func.lower(ProjectRepo.repo) == repo.lower(),
                    ProjectRepo.retired_at.is_not(None)))).scalars().first()
    except Exception as exc:  # noqa: BLE001 - the refusal stands without the hint
        log.info("retired-repo successor lookup failed for %s: %s", repo, exc)
    raise RepoRetired(repo, superseded_by)


def _retired_stmt():
    return select(ProjectRepo.repo).where(ProjectRepo.retired_at.is_not(None))


def _active_stmt(project_id: str):
    return (select(ProjectRepo.repo)
            .where(ProjectRepo.project_id == project_id, ACTIVE)
            .order_by(*REPO_ORDER))


async def retired_repo_set(db: AsyncSession) -> frozenset[str]:
    """Retired repos (casefolded); also refreshes the process snapshot. Callers that
    just changed retirement must ``invalidate_retired_cache()`` AFTER commit rather than
    prime from their own uncommitted session, which would cache state that may roll back."""
    rows = (await db.execute(_retired_stmt())).scalars().all()
    return _store(frozenset(r.casefold() for r in rows if r))


async def active_repos(db: AsyncSession, project_id: str) -> list[str]:
    """Non-retired repos of a product, primary first."""
    return [r for r in (await db.execute(_active_stmt(project_id))).scalars().all() if r]


async def primary_repo(db: AsyncSession, project_id: str) -> str | None:
    return (await db.execute(
        select(ProjectRepo.repo)
        .where(ProjectRepo.project_id == project_id, ProjectRepo.is_primary.is_(True), ACTIVE)
    )).scalars().first()


def active_repos_sync(db: Session, project_id: str) -> list[str]:
    return [r for r in db.execute(_active_stmt(project_id)).scalars().all() if r]
