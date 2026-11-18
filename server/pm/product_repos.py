"""Single source of truth for which repos of a product are live (not retired).

Every queue / sync / clone / filing path asks here instead of reading
``ProjectRepo`` directly. ``is_retired`` serves sync and async hot paths with no DB
session, so it reads a process-local snapshot of the retired set. That snapshot is
refreshed whenever an async/sync ``retired_repo_set*`` read runs (and by Task 2's
mutations); the process has no sync Postgres driver, so ``is_retired`` itself never
touches the DB. It is fail-open: an unprimed or stale-by-error snapshot means "not
retired".
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.db.models import ProjectRepo

log = logging.getLogger(__name__)

# SQL clause: a repo that has not been retired.
ACTIVE = ProjectRepo.retired_at.is_(None)

RETIRED_CACHE_TTL_S = 30.0

_lock = threading.Lock()
_retired_cache: tuple[float, frozenset[str]] | None = None


class RepoRetired(ValueError):
    """A retired repo was used where only an active one is allowed."""

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"Repo {repo} is retired")


def _store(repos: frozenset[str]) -> frozenset[str]:
    global _retired_cache
    with _lock:
        _retired_cache = (time.monotonic(), repos)
    return repos


def invalidate_retired_cache() -> None:
    global _retired_cache
    with _lock:
        _retired_cache = None


def is_retired(repo: str) -> bool:
    """Whether ``repo`` ("owner/name", case-insensitive) is retired, per the last
    snapshot taken within ``RETIRED_CACHE_TTL_S``. Unprimed/expired ⇒ False (fail-open)."""
    with _lock:
        cached = _retired_cache
    if cached is None or time.monotonic() - cached[0] > RETIRED_CACHE_TTL_S:
        return False
    return (repo or "").casefold() in cached[1]


def _retired_stmt():
    return select(ProjectRepo.repo).where(ProjectRepo.retired_at.is_not(None))


def _active_stmt(project_id: str):
    return (select(ProjectRepo.repo)
            .where(ProjectRepo.project_id == project_id, ACTIVE)
            .order_by(ProjectRepo.is_primary.desc(), ProjectRepo.created_at, ProjectRepo.repo))


async def retired_repo_set(db: AsyncSession) -> frozenset[str]:
    rows = (await db.execute(_retired_stmt())).scalars().all()
    return _store(frozenset(r.casefold() for r in rows if r))


def retired_repo_set_sync(db: Session) -> frozenset[str]:
    rows = db.execute(_retired_stmt()).scalars().all()
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


def primary_repo_sync(db: Session, project_id: str) -> str | None:
    return db.execute(
        select(ProjectRepo.repo)
        .where(ProjectRepo.project_id == project_id, ProjectRepo.is_primary.is_(True), ACTIVE)
    ).scalars().first()
