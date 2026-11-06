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
``clone`` is ALWAYS :func:`clone_key` of a path inside the repo — the realpath of the
common git dir's parent — so a session worktree, a linked worktree used as a cwd and
the clone itself all key to the one clone. Every writer and reader goes through it.

``create``/``fork``/``import`` records prove ViberPit made the branch, whichever
session is later removed. A ``backfill`` record only re-states the legacy shape of ONE
row, so it is honoured for that session only.

Branches that predate the registry are covered two ways, both limited to the strict
legacy shape (:func:`legacy_session_branch`): the one-shot startup backfill
(:func:`backfill_session_branches`), and the predicate's own fallback for the session
being removed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import delete as sa_delete, select

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


def clone_top(path: str) -> str | None:
    """Blocking: the common git dir's parent for *path* (a linked worktree, a clone, or
    anywhere inside either), or None when *path* is not in a git repo.

    Combines the is-git-repo probe and the rev-parse into a single blocking call so
    callers can offload both to asyncio.to_thread in one step — git subprocess startup
    is non-trivial under load, and simultaneous archive of many sessions was blocking
    the event loop long enough to trip the health-check watchdog.
    """
    if not worktrees.is_git_repo(path):
        return None
    r = subprocess.run(
        ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return os.path.dirname(r.stdout.strip().rstrip("/"))


def clone_key(path: str) -> str | None:
    """Blocking: THE ``clone`` of a record for *path* — the realpath of
    :func:`clone_top` — or None when *path* is not in a git repo. Never ``--show-toplevel``:
    for a linked worktree that is the worktree, not the clone its branches live in."""
    top = clone_top(path)
    return os.path.realpath(top) if top is not None else None


class BranchRecord(NamedTuple):
    branch: str
    source: str
    session_id: str


# ── the strict legacy shape rule ───────────────────────────────────────────────

def legacy_session_branch(branch: str, session_id: str, *,
                          fork_parent_id: str | None = None,
                          row_branch: str | None = None) -> bool:
    """Whether *branch* has a built-in prefix AND exactly the shape ViberPit gives
    session *session_id*'s own branch — the only proof of ownership an unrecorded
    branch can have.

    A fork child (*fork_parent_id* = the source it was cut from) owns
    ``<prefix>fork-<short source>-<n>`` — and only the ``n`` it was given, i.e. *branch*
    must equal the row's own recorded ``sessions.branch`` (*row_branch*); with no
    *row_branch* there is no telling which ``n`` is its, so it is refused. Any other
    session owns ``<prefix><base>-<short id>``. Strict on purpose: another session's
    shape, a sibling fork's ``n``, an arbitrary ``viberpit/foo``, and a fork-shaped
    branch for a non-fork row (the branch of one of ITS forks) are all refused.
    Derived from the name builders (``worktrees._child_branch`` / ``fork_branch_name``)
    so it cannot drift from what they emit.
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
            return (row_branch is not None and branch == row_branch
                    and rest.startswith(stem) and n.isascii() and n.isdecimal()
                    and str(int(n)) == n and int(n) >= 1)
        suffix = worktrees._child_branch("", session_id)[emitted:]          # -<short>
        return rest.endswith(suffix) and len(rest) > len(suffix)
    return False


# ── record and lookup ──────────────────────────────────────────────────────────

async def record_branch(*, session_id: str, host: str, path: str, branch: str,
                        source: str) -> bool:
    """Record that ViberPit created *branch* in the clone *path* belongs to (any path in
    the repo: the session worktree, the clone; keyed by :func:`clone_key`) on *host*.
    Idempotent (an existing record for the same ``(host, clone, branch)`` stands).

    Never raises on a DB failure or an unresolvable *path*: the branch is then
    unrecorded, so nothing will ever delete it automatically (fail closed), and a
    WARNING says so. Returns whether the record is in place."""
    if source not in SOURCES:
        raise ValueError(f"unknown branch source {source!r}")
    try:
        clone = await asyncio.to_thread(clone_key, path)
        if clone is None:
            raise LookupError(f"not in a git repository: {path}")
        async with db_session() as db:
            await db.execute(
                dialect_insert(SessionBranch).values(
                    id=str(uuid.uuid4()), session_id=session_id, host=host,
                    clone=clone, branch=branch, source=source,
                    created_at=db_utc_now(),
                ).on_conflict_do_nothing(index_elements=_UNIQUE))
    except Exception as exc:
        log_event(log, logging.WARNING, "session_branch.record_failed",
                  session_id=session_id, host=host, branch=branch, source=source,
                  error=type(exc).__name__, detail=str(exc)[:200])
        return False
    return True


async def _records_for(db, host: str, clone: str) -> frozenset[BranchRecord]:
    rows = (await db.execute(
        select(SessionBranch.branch, SessionBranch.source, SessionBranch.session_id)
        .where(SessionBranch.host == host, SessionBranch.clone == clone))).all()
    return frozenset(BranchRecord(r.branch, r.source, r.session_id) for r in rows)


async def branch_records(host: str, path: str) -> frozenset[BranchRecord] | None:
    """Every record for ``(host, clone_key(path))``, or None when the clone or the
    read could not be resolved — which callers must treat as "owns nothing"."""
    try:
        clone = await asyncio.to_thread(clone_key, path)
        if clone is None:
            return None
        async with db_session() as db:
            return await _records_for(db, host, clone)
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=host,
                  error=type(exc).__name__)
        return None


async def source_had_record(session_id: str, branch: str, *, source_host: str | None,
                            target_host: str) -> bool:
    """Did the host a session is moving FROM have a record for *branch*?

    *source_host* must be the name the source RECORDED under — its ``self_host_name()``,
    which the export manifest carries as ``source`` — not the fleet's routing name for
    it (the primary's NULL stamp routes as its registry name or ``"local"``). None
    (unknown) accepts any host but the target. A failed read is False: the target then
    keeps the branch unrecorded (fail closed)."""
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


def predicate_from(records: frozenset[BranchRecord] | None, session_id: str | None, *,
                   fork_parent_id: str | None = None,
                   row_branch: str | None = None) -> Callable[[str], bool]:
    """The ownership predicate over an already-read snapshot of one ``(host, clone)``'s
    records (the janitor reads once per sweep, then runs in a thread). Owned =
    a ``create``/``fork``/``import`` record, a ``backfill`` record for *session_id*
    itself, or the strict legacy shape of *session_id*'s own branch. With no session
    (*session_id* None — a worktree no row maps to) only creation records count: the
    backfill and legacy rules both need the session they vouch for. An unknown
    snapshot (None) owns nothing."""
    if records is None:
        return _never
    proven = frozenset(r.branch for r in records if r.source != SOURCE_BACKFILL
                       or (session_id is not None and r.session_id == session_id))
    if session_id is None:
        return proven.__contains__
    sid = session_id

    def owned(branch: str) -> bool:
        return branch in proven or legacy_session_branch(
            branch, sid, fork_parent_id=fork_parent_id, row_branch=row_branch)
    return owned


class OwnedBranchGate:
    """``remove_worktree``'s ``owned_branch`` predicate for one worktree, which also
    remembers what it approved, so that after the removal :meth:`settle` can forget
    the record of every approved branch that is now gone.

    :meth:`owns` is the pure question (the Discard preflight asks it, so the dialog's
    "will be deleted / kept" is the deletion's own answer); calling the gate asks the
    same question and notes a yes. A predicate that raises owns nothing."""

    def __init__(self, host: str, clone: str | None, owns: Callable[[str], bool]) -> None:
        self.host = host
        self.clone = clone
        self._owns = owns
        self.approved: list[str] = []

    def also_owning(self, created: str | None) -> "OwnedBranchGate":
        """This gate, also owning *created*: a branch the caller ITSELF just made in this
        operation (a spawn rollback undoing its own child branch). That needs no record —
        it is the one deletion the plan allows without one — so a DB that is down cannot
        strand it and wedge every retry of the same session id on "a branch named …
        already exists" (#2255)."""
        if not created:
            return self
        inner = self._owns
        gate = OwnedBranchGate(self.host, self.clone,
                               lambda b: b == created or bool(inner(b)))
        gate.approved = self.approved
        return gate

    def owns(self, branch: str) -> bool:
        if not branch:
            return False
        try:
            return bool(self._owns(branch))
        except Exception:
            return False

    def __call__(self, branch: str) -> bool:
        ok = self.owns(branch)
        if ok:
            self.approved.append(branch)
        return ok

    async def settle(self) -> list[str]:
        """The approved branches git PROVES no longer exist (deleted), each record
        forgotten. Never raises; a branch whose existence cannot be read is neither
        reported deleted nor forgotten."""
        deleted: list[str] = []
        if self.clone is None:
            return deleted
        for branch in dict.fromkeys(self.approved):
            try:
                presence = await asyncio.to_thread(worktrees.branch_presence,
                                                   self.clone, branch)
            except Exception:
                continue
            if presence is False:      # proof of absence only — a git error is unknown
                deleted.append(branch)
                await forget_branch(self.host, self.clone, branch)
        return deleted


async def forget_branch(host: str, clone: str, branch: str) -> None:
    """Drop the record of *branch* (deleted) for ``(host, clone)`` — *clone* is already a
    :func:`clone_key`. Never raises: a record left behind for a deleted branch can only
    vouch for a later same-named branch of the same session."""
    try:
        async with db_session() as db:
            await db.execute(sa_delete(SessionBranch).where(
                SessionBranch.host == host, SessionBranch.clone == clone,
                SessionBranch.branch == branch))
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.forget_failed", host=host,
                  error=type(exc).__name__)


async def forget_if_gone(host: str, path: str, branch: str | None) -> None:
    """Forget *branch*'s record in the clone *path* belongs to when the branch no longer
    exists — for a rollback that deleted a branch it had just made and recorded (a
    fork whose spawn failed). Never raises."""
    if not branch:
        return
    try:
        clone = await asyncio.to_thread(clone_key, path)
        if clone is None or await asyncio.to_thread(worktrees.branch_presence,
                                                    clone, branch) is not False:
            return      # present, or git could not say: keep the record
    except Exception:
        return
    await forget_branch(host, clone, branch)


async def owned_branch_predicate(host: str, path: str, session_id: str) -> OwnedBranchGate:
    """``owned_branch(branch) -> bool`` for removing *session_id*'s worktree from the
    clone *path* belongs to on *host*: that ``(host, clone)``'s records (see
    :func:`predicate_from`), plus the legacy shape of the session's own branch (a
    fork's shape, at its own recorded ``n``, when its row says it is a fork). Any
    failure to resolve the clone or read the DB returns a gate that owns nothing —
    the branch is kept. Build it BEFORE the removal: the clone is resolved from
    *path*, which the removal deletes."""
    try:
        clone = await asyncio.to_thread(clone_key, path)
        if clone is None:
            return OwnedBranchGate(host, None, _never)
        async with db_session() as db:
            records = await _records_for(db, host, clone)
            row = (await db.execute(
                select(DbSession.is_fork, DbSession.parent_session_id, DbSession.branch)
                .where(DbSession.id == session_id))).first()
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=host,
                  error=type(exc).__name__)
        return OwnedBranchGate(host, None, _never)
    fork_parent = row.parent_session_id if row is not None and row.is_fork else None
    return OwnedBranchGate(host, clone, predicate_from(
        records, session_id, fork_parent_id=fork_parent,
        row_branch=row.branch if row is not None else None))


# ── the janitor's once-per-sweep snapshot ──────────────────────────────────────

class _SessionFacts(NamedTuple):
    session_id: str
    fork_parent_id: str | None
    row_branch: str | None


class JanitorOwnership:
    """ONE read of this host's records and the sessions' cwds, taken before the janitor
    sweep enters its thread; :meth:`owned_branch_for` then answers per worktree with no
    DB access. A worktree maps to the session whose ``cwd`` it is (ambiguous → none);
    an unmapped worktree's branch is owned only through a creation record. A failed
    read (``records_by_clone`` None) owns nothing."""

    def __init__(self, host: str,
                 records_by_clone: dict[str, frozenset[BranchRecord]] | None,
                 sessions: list[tuple[str, _SessionFacts]]) -> None:
        self.host = host
        self.records_by_clone = records_by_clone
        self._sessions = sessions
        self._by_path: dict[str, _SessionFacts | None] | None = None
        self.gates: list[OwnedBranchGate] = []

    def _session_at(self, worktree: str) -> _SessionFacts | None:
        if self._by_path is None:      # realpath is filesystem work: in the thread
            by_path: dict[str, _SessionFacts | None] = {}
            for cwd, facts in self._sessions:
                key = os.path.realpath(cwd)
                by_path[key] = None if key in by_path else facts
            self._by_path = by_path
        return self._by_path.get(os.path.realpath(worktree))

    def owned_branch_for(self, worktree: str) -> OwnedBranchGate:
        """Blocking (resolves the clone with git): the gate for *worktree*."""
        if self.records_by_clone is None:
            gate = OwnedBranchGate(self.host, None, _never)
        else:
            clone = clone_key(worktree)
            if clone is None:
                gate = OwnedBranchGate(self.host, None, _never)
            else:
                facts = self._session_at(worktree)
                gate = OwnedBranchGate(self.host, clone, predicate_from(
                    self.records_by_clone.get(clone, frozenset()),
                    facts.session_id if facts else None,
                    fork_parent_id=facts.fork_parent_id if facts else None,
                    row_branch=facts.row_branch if facts else None))
        self.gates.append(gate)
        return gate

    async def settle(self) -> None:
        """Forget the records of every branch the sweep deleted."""
        for gate in self.gates:
            await gate.settle()


async def janitor_ownership(host: str) -> JanitorOwnership:
    """The janitor's snapshot for *host* (see :class:`JanitorOwnership`)."""
    try:
        async with db_session() as db:
            recs = (await db.execute(
                select(SessionBranch.clone, SessionBranch.branch, SessionBranch.source,
                       SessionBranch.session_id).where(SessionBranch.host == host))).all()
            rows = (await db.execute(
                select(DbSession.id, DbSession.cwd, DbSession.is_fork,
                       DbSession.parent_session_id, DbSession.branch)
                .where(DbSession.cwd.is_not(None)))).all()
    except Exception as exc:
        log_event(log, logging.INFO, "session_branch.lookup_failed", host=host,
                  error=type(exc).__name__)
        return JanitorOwnership(host, None, [])
    by_clone: dict[str, set[BranchRecord]] = {}
    for r in recs:
        by_clone.setdefault(r.clone, set()).add(BranchRecord(r.branch, r.source, r.session_id))
    sessions = [(r.cwd, _SessionFacts(r.id, r.parent_session_id if r.is_fork else None,
                                      r.branch)) for r in rows if r.cwd]
    return JanitorOwnership(host, {c: frozenset(v) for c, v in by_clone.items()}, sessions)


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
    on disk, else from its repo's clone path. A candidate whose clone cannot be
    resolved, or whose branch no longer exists there, is dropped — a record for a
    branch that is gone would only vouch for a later same-named one."""
    out: list[tuple[_Candidate, str]] = []
    for c in candidates:
        clone = clone_key(c.cwd) if c.cwd and os.path.isdir(c.cwd) else None
        if clone is None and c.repo:
            path = _clone_path_for(repo_root, c.repo)
            clone = clone_key(str(path)) if path is not None else None
        if clone is not None and worktrees.local_branch_exists(clone, c.branch):
            out.append((c, clone))
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
            r.branch, r.id, fork_parent_id=r.parent_session_id if r.is_fork else None,
            row_branch=r.branch)]
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
    for that row and still exists in its clone. A row whose clone cannot be resolved
    here is skipped. The records are session-scoped (see :func:`predicate_from`). Never raises:
    a failure leaves the branches unrecorded (kept) and retries at the next start.
    Returns how many records it wrote."""
    try:
        return await _backfill(repo_root_fallback)
    except Exception as exc:
        log_event(log, logging.WARNING, "session_branch.backfill_failed",
                  error=type(exc).__name__, detail=str(exc)[:200])
        return 0
