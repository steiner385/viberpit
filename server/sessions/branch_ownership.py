"""Which branches ViberPit created: the branch ownership registry (D-22).

A name match (``brand.is_session_branch``) is not proof that ViberPit made a branch,
and once the prefix is configurable it stops being even a hint. So every branch
ViberPit actually creates is recorded here (``SessionBranch``), and an automatic
``git branch -D`` asks :func:`owned_branch_predicate` instead of the name.

What counts as a creation, and therefore gets recorded:

* ``create_worktree`` returning ``is_child=True`` (the per-session child branch);
* every fork (``create_fork_worktree``);
* a move import that made the branch on the target (``import_`` with no prior ref)
  for a branch the source host had a record for — ownership travels with the move.

Never the user's own branch (``create_worktree`` path 2). A record write that fails
leaves the branch unrecorded — never auto-deleted — and logs a WARNING (fail closed).

Records are scoped by ``(host, clone)``: one DB serves every fleet host, but clones
are per host, so a record on rocky must never vouch for a same-named branch on dobby.
``clone`` is :func:`clone_key` of the clone's toplevel.

Branches that predate the registry are covered two ways, both limited to the strict
legacy shape (:func:`legacy_session_branch`): the one-shot startup backfill
(:func:`backfill_session_branches`), and the predicate's own fallback for the session
being removed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select

from viberpit import brand, gitrepos, worktrees
from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import DataBackfill, Session as DbSession, SessionBranch
from server.hostname import self_host_name
from server.obs.context import log_event

log = logging.getLogger(__name__)

SOURCE_CREATE = "create"
SOURCE_FORK = "fork"
SOURCE_IMPORT = "import"
SOURCE_BACKFILL = "backfill"
SOURCES = frozenset({SOURCE_CREATE, SOURCE_FORK, SOURCE_IMPORT, SOURCE_BACKFILL})

#: ``data_backfills`` marker of :func:`backfill_session_branches`, one per host: each
#: host can only resolve its own clones, so each runs the backfill once for its rows.
_BACKFILL_MARKER = "session_branches:{host}"

_UNIQUE = ["host", "clone", "branch"]


def clone_key(repo_top: str) -> str:
    """The canonical ``clone`` of a record: the realpath of the clone's toplevel (the
    common git dir's parent), so a symlinked repo root and its target are one clone."""
    return os.path.realpath(repo_top)


# ── the strict legacy shape rule ───────────────────────────────────────────────

def legacy_session_branch(branch: str, session_id: str, *,
                          fork_parent_id: str | None = None) -> bool:
    """Whether *branch* has a built-in prefix AND exactly the shape ViberPit gives
    session *session_id*'s own branch — the only proof of ownership an unrecorded
    branch can have.

    A fork child (*fork_parent_id* = the source it was cut from) owns
    ``<prefix>fork-<short source>-<n>``; any other session owns
    ``<prefix><base>-<short id>``. Strict on purpose: another session's shape, an
    arbitrary ``viberpit/foo``, and a fork-shaped branch for a non-fork row (which
    would be the branch of one of ITS forks) are all refused. Derived from the name
    builders (``worktrees._child_branch`` / ``fork_branch_name``) so it cannot drift
    from what they emit.
    """
    if not isinstance(branch, str) or not brand.is_session_branch(branch):
        return False
    emitted = len(brand.BRANCH_PREFIX)
    for prefix in brand.BRANCH_PREFIXES:
        if not branch.startswith(prefix):
            continue
        rest = branch[len(prefix):]
        if fork_parent_id is not None:
            stem = worktrees.fork_branch_name(fork_parent_id, 0)[emitted:-1]  # fork-<s>-
            n = rest[len(stem):]
            return (rest.startswith(stem) and n.isascii() and n.isdecimal()
                    and str(int(n)) == n and int(n) >= 1)
        suffix = worktrees._child_branch("", session_id)[emitted:]          # -<short>
        return rest.endswith(suffix) and len(rest) > len(suffix)
    return False


# ── record and lookup ──────────────────────────────────────────────────────────

async def record_branch(*, session_id: str, host: str, clone: str, branch: str,
                        source: str) -> bool:
    """Record that ViberPit created *branch* in *clone* on *host*. Idempotent (an
    existing record for the same ``(host, clone, branch)`` stands).

    Never raises on a DB failure: the branch is then unrecorded, so nothing will ever
    delete it automatically (fail closed), and a WARNING says so. Returns whether the
    record is in place."""
    if source not in SOURCES:
        raise ValueError(f"unknown branch source {source!r}")
    try:
        async with db_session() as db:
            await db.execute(
                dialect_insert(SessionBranch).values(
                    id=str(uuid.uuid4()), session_id=session_id, host=host,
                    clone=clone_key(clone), branch=branch, source=source,
                    created_at=db_utc_now(),
                ).on_conflict_do_nothing(index_elements=_UNIQUE))
    except Exception as exc:
        log_event(log, logging.WARNING, "session_branch.record_failed",
                  session_id=session_id, host=host, branch=branch, source=source,
                  error=type(exc).__name__, detail=str(exc)[:200])
        return False
    return True


async def recorded_branches(host: str, clone: str) -> frozenset[str] | None:
    """Every recorded branch for ``(host, clone)``, or None when the read failed —
    which callers must treat as "owns nothing"."""
    try:
        async with db_session() as db:
            rows = (await db.execute(
                select(SessionBranch.branch).where(SessionBranch.host == host,
                                                   SessionBranch.clone == clone_key(clone))
            )).scalars().all()
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=host,
                  error=type(exc).__name__)
        return None
    return frozenset(rows)


async def source_had_record(session_id: str, branch: str, *, source_host: str | None,
                            target_host: str) -> bool:
    """Did the host a session is moving FROM have a record for *branch*? *source_host*
    None (unknown) accepts any host but the target. A failed read is False: the
    target then keeps the branch unrecorded (fail closed)."""
    where = [SessionBranch.session_id == session_id, SessionBranch.branch == branch]
    where.append(SessionBranch.host == source_host if source_host
                 else SessionBranch.host != target_host)
    try:
        async with db_session() as db:
            hit = (await db.execute(select(SessionBranch.id).where(*where).limit(1))).first()
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=source_host,
                  error=type(exc).__name__)
        return False
    return hit is not None


# ── the deletion predicate (consumed by remove_worktree's callers) ─────────────

def _never(_branch: str) -> bool:
    return False


def predicate_from(recorded: frozenset[str] | None, session_id: str, *,
                   fork_parent_id: str | None = None) -> Callable[[str], bool]:
    """The ownership predicate over an already-read snapshot (the janitor reads once
    per sweep, then runs in a thread). Owned = recorded, or the strict legacy shape
    of *session_id*'s own branch. An unknown snapshot (None) owns nothing."""
    if recorded is None:
        return _never

    def owned(branch: str) -> bool:
        return branch in recorded or legacy_session_branch(
            branch, session_id, fork_parent_id=fork_parent_id)
    return owned


async def owned_branch_predicate(host: str, clone: str,
                                 session_id: str) -> Callable[[str], bool]:
    """``owned_branch(branch) -> bool`` for removing *session_id*'s worktree from
    *clone* on *host*: the records for that ``(host, clone)``, plus the legacy shape
    of the session's own branch (a fork's shape when its row says it is a fork). Any
    DB failure returns a predicate that owns nothing — the branch is kept."""
    try:
        async with db_session() as db:
            recorded = frozenset((await db.execute(
                select(SessionBranch.branch).where(SessionBranch.host == host,
                                                   SessionBranch.clone == clone_key(clone))
            )).scalars().all())
            row = (await db.execute(
                select(DbSession.is_fork, DbSession.parent_session_id)
                .where(DbSession.id == session_id))).first()
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=host,
                  error=type(exc).__name__)
        return _never
    fork_parent = row.parent_session_id if row is not None and row.is_fork else None
    return predicate_from(recorded, session_id, fork_parent_id=fork_parent)


# ── the one-shot backfill ──────────────────────────────────────────────────────

class _Candidate(NamedTuple):
    session_id: str
    cwd: str | None
    repo: str | None
    branch: str


def _clone_path_for(repo_root: str, repo: str) -> Path | None:
    """The existing local clone of *repo* under *repo_root*, or None."""
    try:
        clone = gitrepos.clone_path(repo_root, repo)
    except ValueError:
        return None
    return clone if (clone / ".git").exists() else None


def _resolve_clones(candidates: Iterable[_Candidate],
                    repo_root: str) -> list[tuple[_Candidate, str]]:
    """Blocking: each candidate's clone key, from its worktree cwd when that is still
    on disk, else from its repo's clone path. Unresolvable candidates are dropped."""
    from server.sessions.orchestrator import _resolve_git_common_dir
    out: list[tuple[_Candidate, str]] = []
    for c in candidates:
        top = _resolve_git_common_dir(c.cwd) if c.cwd and os.path.isdir(c.cwd) else None
        if top is None and c.repo:
            clone = _clone_path_for(repo_root, c.repo)
            top = str(clone) if clone is not None else None
        if top is not None:
            out.append((c, clone_key(top)))
    return out


async def _backfill(repo_root_fallback: str) -> int:
    from server.sessions.orchestrator import effective_repo_root, row_stamped_here
    host = self_host_name()
    marker = _BACKFILL_MARKER.format(host=host)
    async with db_session() as db:
        if await db.get(DataBackfill, marker) is not None:
            return 0
        rows = (await db.execute(
            select(DbSession.id, DbSession.cwd, DbSession.repo, DbSession.branch,
                   DbSession.host, DbSession.is_fork, DbSession.parent_session_id)
            .where(DbSession.branch.is_not(None)))).all()
    candidates = [
        _Candidate(r.id, r.cwd, r.repo, r.branch) for r in rows
        if row_stamped_here(r.host) and legacy_session_branch(
            r.branch, r.id, fork_parent_id=r.parent_session_id if r.is_fork else None)]
    root = await effective_repo_root(repo_root_fallback)
    resolved = await asyncio.to_thread(_resolve_clones, candidates, root)
    async with db_session() as db:
        for c, clone in resolved:
            await db.execute(dialect_insert(SessionBranch).values(
                id=str(uuid.uuid4()), session_id=c.session_id, host=host, clone=clone,
                branch=c.branch, source=SOURCE_BACKFILL, created_at=db_utc_now(),
            ).on_conflict_do_nothing(index_elements=_UNIQUE))
        # Same transaction as the rows; ON CONFLICT because the edge and the daemon on
        # one host can run this concurrently at startup.
        # applied_at is timezone-aware (DataBackfill's convention, like its siblings in
        # server/db/__init__.py), unlike the naive-UTC record timestamps.
        await db.execute(dialect_insert(DataBackfill).values(
            name=marker, applied_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=["name"]))
    log_event(log, logging.INFO, "session_branch.backfilled", host=host,
              recorded=len(resolved), skipped=len(candidates) - len(resolved))
    return len(resolved)


async def backfill_session_branches(*, repo_root_fallback: str) -> int:
    """ONE-SHOT per host (``data_backfills``): record ``source=backfill`` rows for this
    host's live and archived sessions whose ``branch`` passes the strict legacy rule
    for that row. A row whose clone cannot be resolved here is skipped. Never raises:
    a failure leaves the branches unrecorded (kept) and retries at the next start.
    Returns how many records it wrote."""
    try:
        return await _backfill(repo_root_fallback)
    except Exception as exc:
        log_event(log, logging.WARNING, "session_branch.backfill_failed",
                  error=type(exc).__name__, detail=str(exc)[:200])
        return 0
