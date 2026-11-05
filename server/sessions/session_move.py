"""Move an idle Claude session to another fleet host (D-17, decision D19).

Facts: ``docs/superpowers/specs/2026-11-05-d17-move-session-verification.md``; parent
design: ``2026-09-14-cross-host-session-transfer-design.md`` (Phase 1, manual move).

Three halves, one module:

* **The movable predicate** (:func:`movable`) — ONE pure function the API, the menu
  (via ``GET …/move/preflight``) and any later policy (#949, a rebalancer) share. Every
  refusal carries a stable reason code (``MOVE_*``) the UI explains.
* **The edge saga** (:func:`move_session`) — the edge drives it; daemons never talk to
  each other. Claim (``sessions.moving_to`` compare-and-set) → ``transfer_export`` on
  the source → restamp → ``transfer_import`` on the target → ``transfer_finalize`` on
  the source. Any failure after export restores the session to the source. It runs to
  completion even when the HTTP request is cancelled (``aio.run_to_completion``), and
  ``moving_to`` is cleared in every terminal state.
* **The crash reconcile** (:func:`reconcile_stale_moves`) — a ``moving_to`` older than
  :data:`MOVE_STALE_S` belongs to a saga that died with its edge; it is settled by where
  the lease and the worktree actually are, never by guessing.

The daemon halves (``transfer_*``) live on ``SessionOrchestrator``; the transcript
marker helpers below are shared by both sides.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from viberpit.engines import AgentEngine, parse_agent_engine

log = logging.getLogger(__name__)

# ── reason codes ────────────────────────────────────────────────────────────────────
# Predicate refusals (nothing was touched).
MOVE_DISABLED = "MOVE_DISABLED"
MOVE_NO_FLEET = "MOVE_NO_FLEET"
MOVE_NOT_FOUND = "MOVE_NOT_FOUND"
MOVE_ENGINE_UNSUPPORTED = "MOVE_ENGINE_UNSUPPORTED"
MOVE_ASSISTANT = "MOVE_ASSISTANT"
MOVE_NATIVE_DISPLAY = "MOVE_NATIVE_DISPLAY"
MOVE_IN_FLIGHT = "MOVE_IN_FLIGHT"
MOVE_INACTIVE = "MOVE_INACTIVE"
MOVE_NOT_STARTED = "MOVE_NOT_STARTED"
MOVE_MID_TURN = "MOVE_MID_TURN"
MOVE_AWAITING_ANSWER = "MOVE_AWAITING_ANSWER"
MOVE_HAS_CHILDREN = "MOVE_HAS_CHILDREN"
MOVE_IS_CHILD = "MOVE_IS_CHILD"
MOVE_NOT_MANAGED = "MOVE_NOT_MANAGED_WORKTREE"
MOVE_UNKNOWN_HOST = "MOVE_UNKNOWN_HOST"
MOVE_SAME_HOST = "MOVE_SAME_HOST"
MOVE_TARGET_DISABLED = "MOVE_TARGET_DISABLED"
MOVE_TARGET_UNHEALTHY = "MOVE_TARGET_UNHEALTHY"
MOVE_TARGET_ENGINE = "MOVE_TARGET_ENGINE"
MOVE_TARGET_PLATFORM = "MOVE_TARGET_PLATFORM"
MOVE_TARGET_FULL = "MOVE_TARGET_FULL"
MOVE_DIRTY_NEEDS_CONSENT = "MOVE_DIRTY_NEEDS_CONSENT"
MOVE_DIRTY_PUBLIC_REPO = "MOVE_DIRTY_PUBLIC_REPO"
MOVE_BACKGROUND_NEEDS_CONFIRM = "MOVE_BACKGROUND_NEEDS_CONFIRM"
MOVE_SOURCE_UNREACHABLE = "MOVE_SOURCE_UNREACHABLE"
MOVE_TARGET_UNAVAILABLE = "MOVE_TARGET_UNAVAILABLE"
# Saga failures (something was touched; the outcome says where the session is).
MOVE_EXPORT_FAILED = "MOVE_EXPORT_FAILED"
MOVE_RESTAMP_FAILED = "MOVE_RESTAMP_FAILED"
MOVE_IMPORT_FAILED = "MOVE_IMPORT_FAILED"
MOVE_TRANSCRIPT_STALE = "MOVE_TRANSCRIPT_STALE"
MOVE_RESUME_FAILED = "MOVE_RESUME_FAILED"
MOVE_LEASE_HELD = "MOVE_LEASE_HELD"
MOVE_PATH_MISMATCH = "MOVE_PATH_MISMATCH"
MOVE_WORKTREE_MISSING = "MOVE_WORKTREE_MISSING"
MOVE_NOT_STAMPED = "MOVE_NOT_STAMPED"
MOVE_FINALIZE_REFUSED = "MOVE_FINALIZE_REFUSED"
#: The edge guard's code: a per-session call that would build a driver while a move is
#: in flight (HTTP 409, WS error frame).
SESSION_MOVING = "SESSION_MOVING"

#: Outcomes of a saga that got past the claim.
OUTCOME_MOVED = "moved"
OUTCOME_RESTORED = "restored"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"

#: A ``moving_to`` older than this belongs to a saga that died (ruling 2026-11-05: 10
#: min). A live saga's worst case (export push + fetch + resume) is ~2 min.
MOVE_STALE_S = 600.0
#: How often the edge looks for stale claims (the first pass runs at startup).
MOVE_RECONCILE_INTERVAL_S = 60.0
#: Target-side transcript check: attempts, and the pause between them. rocky's sshfs
#: attribute cache can hide the source's last turn for up to 600 s; reading to EOF
#: refreshes it (verified 2026-11-05), so a short retry is all the slack it needs.
TRANSCRIPT_ATTEMPTS = 3
TRANSCRIPT_RETRY_S = 1.0
#: Background tasks listed in a refusal / preflight (the UI's confirm list).
BACKGROUND_LIST_CAP = 20

_HTTP: dict[str, int] = {
    MOVE_NOT_FOUND: 404, MOVE_UNKNOWN_HOST: 400, MOVE_SAME_HOST: 400,
    MOVE_SOURCE_UNREACHABLE: 503, MOVE_TARGET_UNAVAILABLE: 503,
}


class MoveError(Exception):
    """A move refused or failed. ``code`` is a ``MOVE_*`` reason; ``extra`` is merged
    into the HTTP detail (dirty paths, background tasks); ``outcome`` names where the
    session ended up for a failure past the claim. ``error_data`` carries all of it
    across the daemon RPC envelope (``RemoteOrchestrator`` rebuilds the exception)."""

    def __init__(self, code: str, message: str | None = None, *,
                 extra: dict[str, Any] | None = None, outcome: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.extra = dict(extra or {})
        self.outcome = outcome
        self.error_data: dict[str, Any] = {"code": code, **self.extra}
        if outcome is not None:
            self.error_data["outcome"] = outcome

    @property
    def http(self) -> int:
        return _HTTP.get(self.code, 409)

    @classmethod
    def from_error_data(cls, message: str, data: dict | None) -> "MoveError":
        data = dict(data or {})
        code = str(data.pop("code", MOVE_IMPORT_FAILED))
        outcome = data.pop("outcome", None)
        return cls(code, message, extra=data, outcome=outcome)


class SessionMoving(Exception):
    """A per-session call that would build a driver arrived while the session is being
    moved (the edge guard). HTTP 409 / WS ``SESSION_MOVING``."""

    def __init__(self, session_id: str, target: str) -> None:
        super().__init__(f"session {session_id} is moving to {target}")
        self.session_id = session_id
        self.target = target
        self.code = SESSION_MOVING


# ── the predicate ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MoveSubject:
    """The session facts the predicate reads. Built at the edge from the owning host's
    live record (status) and the shared row (lineage, platform, claim)."""

    session_id: str
    engine: AgentEngine
    kind: str | None
    display_mode: str | None
    status: str
    source: str
    moving_to: str | None = None
    platform: str | None = None
    managed: bool = True
    live_children: int = 0
    live_parent: str | None = None


@dataclass(frozen=True)
class WorktreeFacts:
    """What the source daemon reported about the worktree (``transfer_preflight``).
    ``repo_private`` is None when GitHub could not say — treated as public."""

    dirty_count: int = 0
    dirty_paths: tuple[str, ...] = ()
    repo_private: bool | None = None
    background_tasks: tuple[dict, ...] = ()
    #: A live permission / question / plan card (or a re-offered orphaned one) — not
    #: the resting status, which a restart seeds with no card behind it.
    pending_card: bool = False

    @classmethod
    def from_preflight(cls, d: dict) -> "WorktreeFacts":
        return cls(dirty_count=int(d.get("dirtyCount") or 0),
                   dirty_paths=tuple(d.get("dirtyPaths") or ()),
                   repo_private=d.get("repoPrivate"),
                   background_tasks=tuple(d.get("backgroundTasks") or ()),
                   pending_card=bool(d.get("pendingCard")))


@dataclass(frozen=True)
class MoveVerdict:
    ok: bool
    code: str | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.code, "message": self.message, **self.extra}

    def error(self) -> MoveError:
        return MoveError(self.code or MOVE_NOT_FOUND, self.message, extra=self.extra)


def _no(code: str, message: str, **extra: Any) -> MoveVerdict:
    return MoveVerdict(False, code, message, dict(extra))


def movable(subject: MoveSubject, target: str, candidate: Any, *, caps: Any, enabled: bool,
            facts: WorktreeFacts | None = None, carry_dirty: bool = False,
            stop_background: bool = False) -> MoveVerdict:
    """May *subject* be moved to host *target* right now? Pure: every input is passed in.

    *candidate* is the target's ``fleet.HostCandidate`` (None when the router does not
    know the host). *caps* is the engine's ``AgentCapabilities``; *enabled* the kill
    switch. *facts* is the source's worktree report — None (menu-level check) skips the
    dirty / visibility / background legs; the saga always passes them, and the source
    re-checks all three inside ``transfer_export``.
    """
    from server.sessions.display_policy import DISPLAY_NATIVE
    from server.sessions.fleet import engine_rejection, platform_rejection
    from server.sessions.status import MID_TURN_STATUSES
    from server.sessions.types import is_assistant_kind
    from server.db.models import INACTIVE_SESSION_STATUSES

    if not enabled:
        return _no(MOVE_DISABLED, "Moving sessions between hosts is turned off.")
    if caps is None or not caps.cross_host_transfer:
        return _no(MOVE_ENGINE_UNSUPPORTED,
                   "Codex history stays on its host, so a Codex session cannot move."
                   if subject.engine is AgentEngine.CODEX else
                   "This session's engine cannot move between hosts.")
    if is_assistant_kind(subject.kind):
        return _no(MOVE_ASSISTANT, "The assistant always runs on the primary host.")
    if subject.display_mode == DISPLAY_NATIVE:
        return _no(MOVE_NATIVE_DISPLAY,
                   "This session renders on its host's own screen, so it cannot move.")
    if subject.moving_to:
        return _no(MOVE_IN_FLIGHT, f"Already moving to {subject.moving_to}.",
                   movingTo=subject.moving_to)
    if subject.status in INACTIVE_SESSION_STATUSES:
        return _no(MOVE_INACTIVE, "An archived or ended session cannot move.")
    if subject.status in ("queued", "pending_approval"):
        return _no(MOVE_NOT_STARTED, "This session has not started yet.")
    if subject.status in MID_TURN_STATUSES:
        return _no(MOVE_MID_TURN, "Wait for the turn to finish or Interrupt first.")
    if subject.live_children:
        return _no(MOVE_HAS_CHILDREN,
                   "This session is steering child sessions; they run on its host.",
                   children=subject.live_children)
    if subject.live_parent:
        return _no(MOVE_IS_CHILD,
                   "This session is steered by a parent session on its host.",
                   parent=subject.live_parent)
    if not subject.managed:
        return _no(MOVE_NOT_MANAGED,
                   "Only a session in its own managed worktree can move "
                   "(worktrees under the old hidden namespace and scratch sessions cannot).")
    if target == subject.source:
        return _no(MOVE_SAME_HOST, f"The session already runs on {target}.")
    if candidate is None:
        return _no(MOVE_UNKNOWN_HOST, f"{target!r} is not a fleet host.")
    if not candidate.enabled:
        return _no(MOVE_TARGET_DISABLED, f"{target} is disabled.")
    if candidate.healthy is not True:
        return _no(MOVE_TARGET_UNHEALTHY, f"{target} is not healthy right now.")
    why = engine_rejection(candidate, subject.engine)
    if why is not None:
        return _no(MOVE_TARGET_ENGINE, f"{target} cannot run this engine ({why}).")
    why = platform_rejection(candidate, subject.platform)
    if why is not None:
        return _no(MOVE_TARGET_PLATFORM, f"{target} cannot run it ({why}).")
    if (candidate.max_sessions is not None
            and candidate.active_sessions >= candidate.max_sessions):
        return _no(MOVE_TARGET_FULL, f"{target} is at capacity.")
    if facts is not None:
        if facts.pending_card:
            return _no(MOVE_AWAITING_ANSWER,
                       "Answer the session's open question or permission request first.")
        if facts.dirty_count:
            dirty = {"dirtyCount": facts.dirty_count, "dirtyPaths": list(facts.dirty_paths)}
            if facts.repo_private is not True:
                return _no(MOVE_DIRTY_PUBLIC_REPO,
                           "This repository is public (or its visibility is unknown), so "
                           "uncommitted changes cannot travel: commit or discard them first.",
                           **dirty)
            if not carry_dirty:
                return _no(MOVE_DIRTY_NEEDS_CONSENT,
                           "The worktree has uncommitted changes; confirm carrying them.",
                           **dirty)
        if facts.background_tasks and not stop_background:
            return _no(MOVE_BACKGROUND_NEEDS_CONFIRM,
                       "Background tasks will stop and be re-armed on the new host; "
                       "confirm to continue.",
                       backgroundTasks=list(facts.background_tasks))
    return MoveVerdict(True)


def is_under_current_base(cwd: str | None, repo_root: str) -> bool:
    """Is *cwd* a managed worktree under the CURRENT base (``worktrees_base``)? Legacy
    trees under the old hidden namespace are not: ``import_`` recreates only under the current base, so
    their path — and with it the transcript slug — would change on a move."""
    from viberpit import worktrees
    if not cwd:
        return False
    try:
        rel = Path(os.path.abspath(cwd)).relative_to(
            Path(os.path.abspath(worktrees.worktrees_base(repo_root))))
    except ValueError:
        return False
    return len(rel.parts) == 2   # <repo>/<short-id>


# ── transcript marker (source records it, target verifies it) ───────────────────────

def _scan_transcript(path: str, want_uuid: str | None) -> tuple[int, str | None, bool]:
    """Read *path* to EOF: (bytes read, last entry uuid, whether *want_uuid* occurs).
    Reading to EOF is also what refreshes a stale sshfs attribute cache."""
    size = 0
    last_uuid: str | None = None
    found = want_uuid is None
    needle = want_uuid.encode() if want_uuid else b""
    with open(path, "rb") as f:
        for line in f:
            size += len(line)
            stripped = line.strip()
            if not stripped:
                continue
            if needle and not found and needle in stripped:
                found = True
            if b'"uuid"' in stripped:
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    continue
                uid = entry.get("uuid") if isinstance(entry, dict) else None
                if isinstance(uid, str) and uid:
                    last_uuid = uid
    return size, last_uuid, found


def transcript_marker(path: str | None) -> dict | None:
    """``{"size", "lastUuid"}`` of the transcript at *path*, read after the source's
    driver has closed; None when there is no transcript yet (a session that never ran
    a turn has nothing the target could miss)."""
    if not path or not os.path.isfile(path):
        return None
    size, last_uuid, _ = _scan_transcript(path, None)
    return {"size": size, "lastUuid": last_uuid}


def transcript_satisfies(path: str | None, marker: dict | None) -> bool:
    """Does the transcript at *path*, read to EOF here, contain everything the source
    wrote — at least ``marker.size`` bytes and the source's last entry uuid?"""
    if not marker:
        return True
    if not path or not os.path.isfile(path):
        return False
    size, _, found = _scan_transcript(path, marker.get("lastUuid"))
    return size >= int(marker.get("size") or 0) and found


# ── edge: gathering the predicate's inputs ──────────────────────────────────────────

@dataclass
class _Context:
    session_id: str
    source: str
    subject: MoveSubject
    moved_from: str | None
    moved_at: datetime | None


def _require_fleet(sessions: Any) -> Any:
    if not hasattr(sessions, "orch_for_host"):
        raise MoveError(MOVE_NO_FLEET, "Moving a session needs a multi-host fleet.")
    return sessions


async def _row(session_id: str):
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        return (await db.execute(
            sa_select(DbSession).where(DbSession.id == session_id))).scalar_one_or_none()


async def _lineage(row) -> tuple[int, str | None]:
    """(live steering children, live steering parent id) — forks are exempt (D5)."""
    from sqlalchemy import func, or_, select as sa_select
    from server.db import db_session
    from server.db.models import INACTIVE_SESSION_STATUSES, Session as DbSession
    async with db_session() as db:
        children = (await db.execute(
            sa_select(func.count()).select_from(DbSession).where(
                DbSession.parent_session_id == row.id,
                or_(DbSession.is_fork.is_(None), DbSession.is_fork.is_(False)),
                DbSession.status.notin_(INACTIVE_SESSION_STATUSES)))).scalar_one()
        parent: str | None = None
        if row.parent_session_id and not row.is_fork:
            pstatus = (await db.execute(
                sa_select(DbSession.status).where(
                    DbSession.id == row.parent_session_id))).scalar_one_or_none()
            if pstatus is not None and pstatus not in INACTIVE_SESSION_STATUSES:
                parent = row.parent_session_id
    return int(children or 0), parent


async def _context(fleet: Any, session_id: str) -> _Context:
    from server.config import get_settings
    from server.sessions.orchestrator import effective_repo_root
    row = await _row(session_id)
    if row is None:
        raise MoveError(MOVE_NOT_FOUND, f"Session {session_id!r} not found")
    record = await fleet.get_session(session_id)
    source = row.host or fleet.primary_name
    status = getattr(record, "status", None) or row.status
    children, parent = await _lineage(row)
    root = await effective_repo_root(get_settings().repo_root)
    subject = MoveSubject(
        session_id=session_id,
        engine=parse_agent_engine(getattr(row, "engine", None), legacy_default=True),
        kind=row.kind, display_mode=row.display_mode, status=status, source=source,
        moving_to=fleet.moving_target(session_id) or row.moving_to,
        platform=row.platform, managed=is_under_current_base(row.cwd, root),
        live_children=children, live_parent=parent)
    return _Context(session_id, source, subject, row.moved_from, row.moved_at)


async def _candidate(fleet: Any, target: str):
    for c in await fleet.host_candidates():
        if c.name == target:
            return c
    return None


async def _facts(fleet: Any, ctx: _Context) -> WorktreeFacts:
    try:
        report = await fleet.orch_for_host(ctx.source).transfer_preflight(ctx.session_id)
    except MoveError:
        raise
    except Exception as exc:
        raise MoveError(MOVE_SOURCE_UNREACHABLE,
                        f"{ctx.source} did not answer: {type(exc).__name__}") from exc
    return WorktreeFacts.from_preflight(report or {})


async def _verdict(fleet: Any, ctx: _Context, target: str, facts: WorktreeFacts | None, *,
                   carry_dirty: bool, stop_background: bool) -> MoveVerdict:
    from server.sessions.engine import declared_capabilities, session_move_enabled
    return movable(ctx.subject, target, await _candidate(fleet, target),
                   caps=declared_capabilities(ctx.subject.engine),
                   enabled=session_move_enabled(), facts=facts,
                   carry_dirty=carry_dirty, stop_background=stop_background)


async def preflight(sessions: Any, session_id: str, target: str | None = None) -> dict:
    """What the move dialog shows before posting: the verdict for *target* (or the
    session-level verdict when None), the per-host verdicts for every other fleet host,
    the dirty file list, the repo's visibility and the background tasks that will stop.
    Read-only."""
    fleet = _require_fleet(sessions)
    ctx = await _context(fleet, session_id)
    facts: WorktreeFacts | None = None
    facts_error: str | None = None
    try:
        facts = await _facts(fleet, ctx)
    except MoveError as exc:
        facts_error = exc.code
    hosts = []
    for name in fleet.host_names():
        if name == ctx.source:
            continue
        v = await _verdict(fleet, ctx, name, None, carry_dirty=False, stop_background=False)
        hosts.append({"name": name, "ok": v.ok, "reason": v.code, "message": v.message})
    out: dict[str, Any] = {
        "sessionId": session_id, "source": ctx.source, "hosts": hosts,
        "dirty": ({"count": facts.dirty_count, "paths": list(facts.dirty_paths)}
                  if facts is not None else None),
        "repoPrivate": facts.repo_private if facts is not None else None,
        "backgroundTasks": list(facts.background_tasks) if facts is not None else [],
        "factsError": facts_error,
    }
    if target is not None:
        # Consent and confirm are what the dialog collects, so they are assumed here:
        # what remains is whether the move is possible at all.
        v = await _verdict(fleet, ctx, target, facts, carry_dirty=True, stop_background=True)
        out["target"] = target
        out["verdict"] = v.to_wire()
    else:
        # Session level: movable somewhere, else the first host's reason.
        first = next((h for h in hosts if h["ok"]), hosts[0] if hosts else None)
        out["verdict"] = ({"ok": first["ok"], "reason": first["reason"],
                           "message": first["message"]} if first is not None
                          else _no(MOVE_UNKNOWN_HOST, "No other fleet host.").to_wire())
    return out


# ── edge: DB writes ─────────────────────────────────────────────────────────────────

async def _claim(session_id: str, target: str) -> bool:
    """``UPDATE … SET moving_to=:t WHERE id=:id AND moving_to IS NULL`` — the CAS."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.clock import db_utc_now
    from server.db.models import Session as DbSession
    async with db_session() as db:
        res = await db.execute(
            sa_update(DbSession)
            .where(DbSession.id == session_id, DbSession.moving_to.is_(None))
            .values(moving_to=target, move_started_at=db_utc_now()))
        await db.commit()
    return (res.rowcount or 0) == 1  # type: ignore[attr-defined]  # CursorResult at runtime


async def _release_claim(session_id: str) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                         .values(moving_to=None, move_started_at=None,
                                 move_imported_by=None))
        await db.commit()


async def _abandon_claim(session_id: str) -> None:
    """Keep the claim but mark it abandoned (no start time): the reconcile takes it on
    its next pass instead of waiting out :data:`MOVE_STALE_S`."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                         .values(move_started_at=None))
        await db.commit()


async def mark_import_complete(session_id: str, host: str) -> None:
    """The target's import-complete evidence (written by ``transfer_import`` once its
    driver started on a verified transcript). The crash reconcile trusts this, never
    the mere presence of a directory."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(
            DbSession.id == session_id, DbSession.moving_to == host)
            .values(move_imported_by=host))
        await db.commit()


_NOW = object()


async def _restamp(session_id: str, *, host: str, moved_from: str | None,
                   moved_at: Any) -> None:
    """Point the row at *host*. ``moved_at=_NOW`` stamps the database clock."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.clock import db_utc_now
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id).values(
            host=host, moved_from=moved_from,
            moved_at=db_utc_now() if moved_at is _NOW else moved_at))
        await db.commit()


async def _fresh_lease_holder(session_id: str) -> str | None:
    """The host holding the session's lease with a live heartbeat, else None. The lease
    is the one FACT about who is driving a session (#1379)."""
    from server.sessions import session_lease
    from server.sessions.orchestrator import SESSION_LEASE_TTL_S
    try:
        state = await session_lease.current(session_id)
    except Exception:
        log.info("session.move lease read failed id=%s", session_id, exc_info=True)
        return None
    if state is None or state.age_s >= SESSION_LEASE_TTL_S:
        return None
    return state.holder


async def _audit(identity: str, via: str | None, detail: dict) -> None:
    from server.audit import record_audit
    from server.db import db_session
    try:
        async with db_session() as db:
            await record_audit(db, "session.move", identity, detail, via=via)
            await db.commit()
    except Exception:
        log.warning("session.move audit write failed detail=%s", detail, exc_info=True)


# ── edge: the saga ──────────────────────────────────────────────────────────────────

async def move_session(sessions: Any, session_id: str, target: str, *, identity: str,
                       via: str | None = None, carry_dirty: bool = False,
                       stop_background: bool = False) -> dict:
    """Move *session_id* to fleet host *target*. Raises :class:`MoveError` for every
    refusal (nothing touched) and every failure (``outcome`` says where the session is:
    ``restored`` = back on the source, ``failed`` = neither host could resume it — its
    row and worktree are intact on the source). Returns the result on success.

    Everything past the predicate runs under ``run_to_completion``: a cancelled HTTP
    request must not leave a half-moved session."""
    from server.aio import run_to_completion
    fleet = _require_fleet(sessions)
    ctx = await _context(fleet, session_id)
    facts = await _facts(fleet, ctx)
    verdict = await _verdict(fleet, ctx, target, facts, carry_dirty=carry_dirty,
                             stop_background=stop_background)
    if not verdict.ok:
        log.info("session.move_refused id=%s from=%s to=%s code=%s",
                 session_id, ctx.source, target, verdict.code)
        await _audit(identity, via, {"sessionId": session_id, "from": ctx.source,
                                     "to": target, "carryDirty": carry_dirty,
                                     "outcome": OUTCOME_REFUSED, "code": verdict.code})
        raise verdict.error()
    return await run_to_completion(
        _saga(fleet, ctx, target, identity=identity, via=via, carry_dirty=carry_dirty,
              stop_background=stop_background),
        name=f"session-move-{session_id}")


def _orch(fleet: Any, host: str, code: str = MOVE_TARGET_UNAVAILABLE) -> Any:
    """``fleet.orch_for_host`` with a coded refusal instead of a bare ``UnknownHost``
    (a host the registry lists but this edge holds no client for)."""
    from server.sessions.fleet_router import UnknownHost
    try:
        return fleet.orch_for_host(host)
    except UnknownHost as exc:
        raise MoveError(code, f"{host} is not reachable from this edge.") from exc


async def _saga(fleet: Any, ctx: _Context, target: str, *, identity: str, via: str | None,
                carry_dirty: bool, stop_background: bool) -> dict:
    sid, source = ctx.session_id, ctx.source
    # Both clients before the claim: a host this edge cannot reach is a refusal that
    # touched nothing.
    src = _orch(fleet, source, MOVE_SOURCE_UNREACHABLE)
    tgt = _orch(fleet, target)
    if not await _claim(sid, target):
        log.info("session.move_refused id=%s code=%s", sid, MOVE_IN_FLIGHT)
        raise MoveError(MOVE_IN_FLIGHT, "Another move of this session is in flight.")
    fleet.begin_move(sid, target)
    started = time.monotonic()
    outcome, code = OUTCOME_FAILED, None
    finalize: dict | None = None
    try:
        manifest: dict | None = None
        try:
            manifest = await src.transfer_export(
                sid, target=target, carry_dirty=carry_dirty, stop_background=stop_background)
        except MoveError as exc:
            if exc.code != MOVE_EXPORT_FAILED:
                # Refused before the source touched anything (a prompt landed, the tree
                # got dirty, …): the session never left.
                outcome, code = OUTCOME_REFUSED, exc.code
                log.info("session.move_refused id=%s by=%s code=%s", sid, source, exc.code)
                raise
            code = exc.code
            outcome = await _restore(fleet, sid, source, target, None, ctx, restamped=False)
            raise MoveError(code, exc.message, extra=exc.extra, outcome=outcome) from exc
        except Exception as exc:
            code = MOVE_EXPORT_FAILED
            outcome = await _restore(fleet, sid, source, target, None, ctx, restamped=False)
            raise MoveError(code, f"export on {source} failed: {exc}", outcome=outcome) from exc
        try:
            await _restamp(sid, host=target, moved_from=source, moved_at=_NOW)
        except Exception as exc:
            code = MOVE_RESTAMP_FAILED
            outcome = await _restore(fleet, sid, source, target, manifest, ctx, restamped=True)
            raise MoveError(code, f"restamp failed: {exc}", outcome=outcome) from exc
        fleet.note_host(sid, target)
        try:
            await tgt.transfer_import(sid, manifest, source=source)
        except Exception as exc:
            code = exc.code if isinstance(exc, MoveError) else MOVE_IMPORT_FAILED
            msg = exc.message if isinstance(exc, MoveError) else f"import on {target} failed: {exc}"
            log.warning("session.move import failed id=%s %s→%s code=%s: %s",
                        sid, source, target, code, msg)
            outcome = await _restore(fleet, sid, source, target, manifest, ctx, restamped=True)
            raise MoveError(code, msg, outcome=outcome) from exc
        outcome = OUTCOME_MOVED
        finalize = await _finalize(fleet, sid, source, target, manifest)
        log.info("session.moved id=%s %s→%s carry_dirty=%s in %.1fs", sid, source, target,
                 carry_dirty, time.monotonic() - started)
        return {"sessionId": sid, "from": source, "to": target, "outcome": outcome,
                "finalized": bool(finalize.get("removed")),
                "recoveryRef": finalize.get("recoveryRef")}
    finally:
        fleet.end_move(sid)
        if outcome == OUTCOME_FAILED:
            # Neither host took the session back: the source may still hold the WIP
            # commit and the refs are still on GitHub. Leave the claim for the reconcile
            # (marked abandoned, so it runs on the next pass, not in 10 min) and keep
            # refusing driver builds until it has resolved where the session lives.
            fleet.hold_move(sid, target)
            try:
                await _abandon_claim(sid)
            except Exception:
                log.warning("session.move could not mark the claim abandoned id=%s",
                            sid, exc_info=True)
            log.warning("session.move failed id=%s %s→%s code=%s — left to the reconcile",
                        sid, source, target, code)
        else:
            try:
                await _release_claim(sid)
            except Exception:
                log.warning("session.move could not clear moving_to id=%s — the reconcile "
                            "will", sid, exc_info=True)
        await _audit(identity, via, {
            "sessionId": sid, "from": source, "to": target, "carryDirty": carry_dirty,
            "stopBackground": stop_background, "outcome": outcome, "code": code,
            "finalized": bool(finalize and finalize.get("removed")),
            "recoveryRef": (finalize or {}).get("recoveryRef"),
            "durationMs": int((time.monotonic() - started) * 1000)})


async def _restore(fleet: Any, sid: str, source: str, target: str, manifest: dict | None,
                   ctx: _Context, *, restamped: bool) -> str:
    """Put the session back on *source* after a failure past export: restamp back, then
    the source's own reuse-path import (its worktree is still there). If the source
    cannot take it back, the lease decides: a target that is driving it keeps it, and
    the source's copy is finalized so nothing there can resume it."""
    if restamped:
        try:
            await _restamp(sid, host=source, moved_from=ctx.moved_from, moved_at=ctx.moved_at)
        except Exception:
            log.warning("session.move restamp-back failed id=%s", sid, exc_info=True)
    fleet.note_host(sid, source)
    try:
        await _orch(fleet, source, MOVE_SOURCE_UNREACHABLE).transfer_import(
            sid, manifest, source=target, local=True)
        log.info("session.move restored id=%s on %s", sid, source)
        return OUTCOME_RESTORED
    except Exception:
        log.warning("session.move restore on %s failed id=%s", source, sid, exc_info=True)
    holder = await _fresh_lease_holder(sid)
    if holder == target:
        # The target answered too late for the edge but is driving it now: it is the
        # target's session. Never restore over a live twin — and drop the source's
        # copy, which the failed restore may have unpacked and re-recorded.
        try:
            await _restamp(sid, host=target, moved_from=source, moved_at=_NOW)
        except Exception:
            log.warning("session.move restamp to lease holder failed id=%s", sid,
                        exc_info=True)
            return OUTCOME_FAILED
        fleet.note_host(sid, target)
        log.warning("session.move id=%s: restore failed but %s holds the lease — kept "
                    "there", sid, target)
        await _finalize(fleet, sid, source, target, manifest)
        return OUTCOME_MOVED
    return OUTCOME_FAILED


async def _finalize(fleet: Any, sid: str, source: str, target: str,
                    manifest: dict | None) -> dict:
    """Drop the source's copy (D4: only now that the target's driver has started). A
    failure is audited but does not change the outcome: the session is live on the
    target, and the leftover tree is stamped for another host so nothing resumes it."""
    try:
        return await _orch(fleet, source, MOVE_SOURCE_UNREACHABLE).transfer_finalize(
            sid, manifest, owner=target) or {}
    except Exception as exc:
        log.warning("session.move finalize on %s failed id=%s — the source worktree "
                    "stays (stamped for %s)", source, sid, target, exc_info=True)
        return {"removed": False, "error": getattr(exc, "code", type(exc).__name__)}


# ── edge: crash reconcile ───────────────────────────────────────────────────────────

async def reconcile_stale_moves(sessions: Any, *, older_than_s: float = MOVE_STALE_S
                                ) -> list[str]:
    """Settle every claim older than *older_than_s* (or abandoned: no start time) that no
    live saga in this process owns. Every claim this edge is not running is also HELD —
    driver builds answer SESSION_MOVING — until it is settled. Returns the ids settled.
    Never raises."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.clock import naive_utc_now
    from server.db.models import Session as DbSession
    fleet = _require_fleet(sessions)
    cutoff = naive_utc_now() - timedelta(seconds=older_than_s)
    try:
        async with db_session() as db:
            rows = (await db.execute(sa_select(DbSession).where(
                DbSession.moving_to.isnot(None)))).scalars().all()
    except Exception:
        log.warning("session.move reconcile: claim scan failed", exc_info=True)
        return []
    settled: list[str] = []
    for row in rows:
        if fleet.moving_target(row.id) is not None:
            continue   # a live saga in this edge
        fleet.hold_move(row.id, row.moving_to)
        if row.move_started_at is not None and row.move_started_at >= cutoff:
            continue   # maybe a live saga in another edge: not stale yet
        try:
            if await _reconcile_one(fleet, row):
                settled.append(row.id)
        except Exception:
            log.warning("session.move reconcile failed id=%s", row.id, exc_info=True)
    return settled


async def _reconcile_one(fleet: Any, row) -> bool:
    """Decide where the session lives and make it true, in D4 order: the winner resumes
    (and is verified) FIRST; only then is the other side's copy dropped.

    Winner: a host holding a fresh lease; else the target only with import-complete
    evidence (``move_imported_by`` — written after its transcript check and driver
    start, never just "a directory exists"); else the source. If the winner cannot
    resume, fall back to the other side when it is eligible (the source always; the
    target only with that evidence)."""
    sid, target = row.id, row.moving_to
    restamped = row.host == target
    source = (row.moved_from if restamped else row.host) or fleet.primary_name
    holder = await _fresh_lease_holder(sid)
    imported = getattr(row, "move_imported_by", None) == target
    first: str
    if holder is not None and holder in (source, target):
        first = holder
    else:
        first = target if imported else source
    order: list[str] = [first]
    other = source if first == target else target
    if other == source or imported:
        order.append(other)
    log.warning("session.move_reconcile id=%s claim=%s→%s stamp=%s lease=%s imported=%s "
                "order=%s", sid, source, target, row.host, holder, imported, order)
    for cand in order:
        loser = target if cand == source else source
        if cand == target:
            await _restamp(sid, host=target, moved_from=source,
                           moved_at=row.moved_at if restamped else _NOW)
        elif restamped:
            # The restamp to the target is being undone; what the row said before it
            # is not known any more, so the "last move" fields are cleared.
            await _restamp(sid, host=source, moved_from=None, moved_at=None)
        else:
            await _restamp(sid, host=source, moved_from=row.moved_from, moved_at=row.moved_at)
        fleet.note_host(sid, cand)
        if cand != holder:
            try:
                await _orch(fleet, cand).transfer_import(sid, None, source=loser, local=True)
            except Exception:
                log.warning("session.move reconcile: %s could not resume id=%s", cand, sid,
                            exc_info=True)
                continue
        try:
            await _orch(fleet, loser).transfer_finalize(sid, None, owner=cand)
        except Exception:
            log.info("session.move reconcile: dropping %s's copy failed id=%s", loser, sid,
                     exc_info=True)
        await _release_claim(sid)
        fleet.release_hold(sid)
        await _audit("system", None, {"sessionId": sid, "from": source, "to": target,
                                      "outcome": f"reconciled:{cand}", "lease": holder})
        return True
    # Nobody could resume it: back to the source's stamp (its tree was never removed),
    # still claimed and held, for the next pass.
    if order[-1] != source:
        await _restamp(sid, host=source, moved_from=None if restamped else row.moved_from,
                       moved_at=None if restamped else row.moved_at)
        fleet.note_host(sid, source)
    await _abandon_claim(sid)
    log.warning("session.move reconcile: no host could resume id=%s — retrying next pass",
                sid)
    return False


async def reconcile_loop(sessions: Any, interval_s: float = MOVE_RECONCILE_INTERVAL_S) -> None:
    """Run :func:`reconcile_stale_moves` at startup and then every *interval_s*."""
    while True:
        try:
            await reconcile_stale_moves(sessions)
        except Exception:
            log.warning("session.move reconcile tick failed", exc_info=True)
        await asyncio.sleep(interval_s)
