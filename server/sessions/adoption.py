"""Fenced adoption of sessions off dead hosts, and their resume on the new host
(spec 002 FR-201..FR-205, issue #949).

Two halves on two daemons, meeting at one row marker (``sessions.adopted_from``):

* **The adoption pass** (:func:`adopt_host`, driven by the PRIMARY's
  ``_adoption_loop`` every :data:`ADOPTION_INTERVAL_S`) — FR-201/203/205. For every
  host whose heartbeat is older than ``VIBERPIT_HOST_DEAD_AFTER_S`` and which still
  owns a non-terminal session, it FIRST bumps that host's fence
  (``host_heartbeat.declare_dead``, a compare-and-swap: exactly one winner, and a
  host whose beat turned out to be fresh after all is never fenced — NFR-3), then per
  orphaned session: an ``interrupted{host_lost, fromStatus}`` event, the SAME
  normalization a daemon restart applies (``restart_normalized_status``), and one
  conditional UPDATE that restamps ``host`` to the best healthy host by the fleet's
  ranked-host logic and sets ``adopted_from``. The primary never spawns anything here:
  a daemon cannot drive a session on another host, and the target may be itself.

* **The pickup** (:func:`pickup_adopted`, every daemon, on the always-on tick) —
  FR-204/205. A row stamped for THIS host with ``adopted_from`` set is one the pass
  handed over and nobody has taken yet. :func:`take_adopted` installs it, reconciles
  the holds the dead host left open (``hold_lost``, FR-305), then decides by the same
  resume policy a restart uses (``_resume_eligible``: hold-aware, honours
  ``auto_resume_opt_out``): a session that was mid-turn or waiting on a human is
  resumed from its transcript when the per-account config dir has it (the dir is one
  shared tree across hosts — see ``specs/002-session-continuity/research.md``), else
  started fresh with a continuity prompt naming the lost host, repo, branch, goal and
  last prompt; a resting session is left detached and resumes on its next prompt; a
  host at capacity queues it (``session_queue`` entry with ``adopted_resume``) and the
  drain resumes it when a slot frees. Every outcome records one ``moved_host`` event
  and clears the marker — so a target that dies before taking a row leaves the marker
  for the next pass to re-adopt elsewhere.

The fenced host's side (stand-down) is ``SessionOrchestrator._on_fenced`` (FR-202).
Nothing here raises into its loop: every entry point logs and returns.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from viberpit.engines import AgentEngine, parse_agent_engine
from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import INACTIVE_SESSION_STATUSES, Host, Session as DbSession
from server.obs.context import log_event
from server.sessions import host_heartbeat
from server.sessions import provenance as _provenance
from server.sessions.fleet import HostCandidate, rank_hosts
from server.sessions.host_engines import parse_engines_info
from server.sessions.host_heartbeat import HeartbeatState
from server.sessions.host_platforms import parse_platforms_info
from server.sessions.types import SYSTEM_IDENTITY, is_assistant

log = logging.getLogger(__name__)

#: How often the primary looks for dead hosts (plan Task 18: "60 s loop").
ADOPTION_INTERVAL_S = 60.0

#: ``session_queue.params`` flag of an adopted session the target queued for capacity
#: (FR-205): the drain resumes it through :func:`take_adopted` instead of spawning.
ADOPTED_RESUME_PARAM = "adopted_resume"
QUEUE_REASON = "host_capacity"

#: ``moved_host.resume`` vocabulary (see ``provenance.MOVED_HOST``).
RESUME_TRANSCRIPT = "transcript"
RESUME_FRESH = "fresh"
RESUME_QUEUED = "queued"
RESUME_DEFERRED = "deferred"
RESUME_FAILED = "failed"

#: Sent as the first prompt on the new host when the transcript was there (FR-204);
#: the re-run sentence is the restart nudge's (``RESUME_RERUN_NOTE_TEMPLATE``), so the
#: two cannot drift.
MOVED_RESUME_NUDGE = ("This session was moved from {from_host} to {to_host} after "
                      "{from_host} became unreachable. {rerun} Continue where you left off.")


def moved_resume_nudge(*, from_host: str, to_host: str) -> str:
    from server.sessions.orchestrator import RESUME_RERUN_NOTE_TEMPLATE
    return MOVED_RESUME_NUDGE.format(
        from_host=from_host, to_host=to_host,
        rerun=RESUME_RERUN_NOTE_TEMPLATE.format(cause=f"{from_host} going down"))


#: Sent as the first prompt of the FRESH conversation when no transcript was reachable.
CONTINUITY_PROMPT = (
    "This session was moved from {from_host} to {to_host} after {from_host} became "
    "unreachable; its conversation could not be recovered. Repo: {repo}, branch: {branch} "
    "(unpushed work may still exist on {from_host}). The goal was: {title}. The last "
    "prompt was:\n\n{prompt}\n\nRe-establish context and continue.")
#: Prepended to the next prompt of a resting session that was adopted (no eager turn).
MOVED_NOTICE = ("This session was moved from {from_host} to {to_host} after {from_host} "
                "became unreachable.")
MOVED_NOTICE_NO_TRANSCRIPT = (
    " Its previous conversation could not be recovered, so this is a new one; "
    "unpushed work may still exist on {from_host} (branch {branch}).")
#: The continuity prompt's quote of the last prompt is bounded: a 30 KB paste is not
#: context, it is the whole previous turn again.
LAST_PROMPT_MAX_CHARS = 2000


# ── the adoption pass (primary) ───────────────────────────────────────────────────────

async def find_dead_hosts(*, dead_after_s: float, exclude: frozenset[str] = frozenset(),
                          ) -> list[HeartbeatState]:
    """Hosts whose heartbeat is older than *dead_after_s* (DB clock, FR-102) AND which
    still own at least one non-terminal session. A stale host owning nothing is left
    alone: there is nothing to adopt and fencing it would only churn its fence.
    *exclude* is the caller itself (a primary never adopts off itself)."""
    snap = await host_heartbeat.snapshot()
    stale = {name: st for name, st in snap.hosts.items()
             if st.age_s > dead_after_s and name not in exclude}
    if not stale:
        return []
    async with db_session() as db:
        owning = {h for h in (await db.execute(
            sa_select(DbSession.host).where(
                DbSession.host.in_(list(stale)),
                DbSession.status.notin_(INACTIVE_SESSION_STATUSES),
            ).distinct())).scalars().all() if h is not None}
    return [stale[name] for name in sorted(owning)]


async def orphaned_rows(host: str) -> list[Any]:
    """The non-terminal sessions stamped for *host*, oldest first. A row mid-move
    (``moving_to`` set) belongs to the move saga's own reconcile, never to adoption."""
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(
                DbSession.host == host,
                DbSession.status.notin_(INACTIVE_SESSION_STATUSES),
                DbSession.moving_to.is_(None),
            ).order_by(DbSession.created_at))).scalars().all()
    return list(rows)


@dataclass
class _Targets:
    """Ranking inputs for one pass, with the slots this pass has already handed out
    so five sessions off one dead host spread by headroom instead of all landing on
    the host that ranked first before any of them arrived."""
    candidates: list[HostCandidate]
    taken: dict[str, int]

    def pick(self, *, engine: AgentEngine | None, platform: str | None) -> str | None:
        current = [HostCandidate(
            name=c.name, enabled=c.enabled, healthy=c.healthy, weight=c.weight,
            active_sessions=c.active_sessions + self.taken.get(c.name, 0),
            max_sessions=c.max_sessions, load_avg=c.load_avg,
            mem_available_mb=c.mem_available_mb, engines=c.engines, platforms=c.platforms,
        ) for c in self.candidates]
        ranked = rank_hosts(current, engine=engine, platform=platform)
        if not ranked:
            # Every able host is FULL: the session still needs an owner, so it goes to
            # the least-loaded one and lands `queued` there (FR-205) rather than staying
            # stamped for a host that will never run it.
            ranked = rank_hosts(current, engine=engine, platform=platform,
                                ignore_capacity=True)
        if not ranked:
            return None
        self.taken[ranked[0]] = self.taken.get(ranked[0], 0) + 1
        return ranked[0]


async def candidate_targets(*, dead_after_s: float, exclude: frozenset[str]) -> _Targets:
    """Adoption targets: enabled registry hosts the edge probe reports healthy AND whose
    own heartbeat is fresh enough not to be adoptable themselves. Missing heartbeat
    row = not a target (absence of evidence is fine for *not demoting* a host, NFR-3;
    it is not enough to hand it work)."""
    snap = await host_heartbeat.snapshot()
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host))).scalars().all()
    candidates = []
    for r in rows:
        if r.name in exclude:
            continue
        hb = snap.hosts.get(r.name)
        alive = hb is not None and hb.age_s <= dead_after_s
        candidates.append(HostCandidate(
            name=r.name, enabled=r.enabled, healthy=(r.healthy is True and alive),
            weight=r.weight, active_sessions=r.active_sessions or 0,
            max_sessions=r.max_sessions, load_avg=r.load_avg,
            mem_available_mb=r.mem_available_mb,
            engines=parse_engines_info(r.engines),
            platforms=parse_platforms_info(r.platforms)))
    return _Targets(candidates=candidates, taken={})


async def _restamp_adopted(row, *, target: str, from_host: str, status: str) -> bool:
    """The one write that moves a session off a dead host: restamp + normalize +
    marker, conditional on the row still being where and what the pass read it as, so
    a session that moved, ended or changed status meanwhile is left exactly alone.

    A row that was still ``queued`` on the dead host gets no marker: nothing of it ran
    there, so there is nothing to pick up — the shared queue's normal drain starts it
    on whichever host has room, exactly as it would have."""
    marker = None if row.status == "queued" else from_host
    async with db_session() as db:
        res = await db.execute(
            sa_update(DbSession)
            .where(DbSession.id == row.id, DbSession.host == from_host,
                   DbSession.status == row.status, DbSession.moving_to.is_(None))
            .values(host=target, status=status, pid=None, background_work=None,
                    moved_from=from_host, moved_at=db_utc_now(), adopted_from=marker))
    return (res.rowcount or 0) == 1  # type: ignore[attr-defined]  # CursorResult at runtime


async def adopt_host(orch, hb: HeartbeatState, *, dead_after_s: float) -> int:
    """Fence *hb*'s host and hand its orphaned sessions to healthy hosts (FR-201/203/205).
    Returns how many sessions were restamped; 0 when the fence CAS lost (the beat
    turned out fresh, or another pass got there first) — the one-winner rule."""
    from server.sessions.orchestrator import restart_normalized_status, self_host_name
    # Targets BEFORE the fence: with nowhere to put the sessions, fencing the host only
    # churns its fence (and an auto-filed WARNING) every pass while changing nothing —
    # the rows read "Unreachable" truthfully until a host is up to take them.
    targets = await candidate_targets(dead_after_s=dead_after_s,
                                      exclude=frozenset({hb.host}))
    if not any(c.enabled and c.healthy for c in targets.candidates):
        log.info("adoption: %r is past the dead threshold (%.0fs) but no healthy host can "
                 "take its sessions — not fencing it this pass", hb.host, hb.age_s)
        return 0
    new_fence = await host_heartbeat.declare_dead(
        hb.host, dead_after_s=dead_after_s, observed_fence=hb.fence)
    if new_fence is None:
        log.info("adoption: %r not fenced (beat fresh again, or already fenced) — skipping",
                 hb.host)
        return 0
    rows = await orphaned_rows(hb.host)
    # WARNING once per dead host (it is an incident), INFO per session: one issue for
    # the outage, not one per card.
    log_event(log, logging.WARNING, "host.declared_dead", host=hb.host,
              beat_age_s=round(hb.age_s), fence=new_fence, sessions=len(rows))
    adopted = 0
    for row in rows:
        engine = parse_agent_engine(getattr(row, "engine", None), legacy_default=True)
        target = targets.pick(engine=engine, platform=getattr(row, "platform", None))
        if target is None:
            # Hosts are up, but none runs this engine / platform: left in place.
            log.info("adoption: no able host for session %s (engine=%s, platform=%s) off "
                     "dead host %r — left in place", row.id, engine.value,
                     getattr(row, "platform", None), hb.host)
            continue
        from_status = row.status
        normalized = restart_normalized_status(from_status)
        # The CAS first, the event second: a row that changed under the pass gets no
        # `interrupted` entry it never earned.
        if not await _restamp_adopted(row, target=target, from_host=hb.host,
                                      status=normalized):
            log.info("adoption: session %s changed under the pass — not restamped", row.id)
            continue
        await _provenance.record(row.id, _provenance.INTERRUPTED, {
            "kind": _provenance.INTERRUPTED_HOST_LOST, "fromStatus": from_status,
            "host": hb.host})
        adopted += 1
        # The primary's dashboard mirror of the row (it holds every host's rows) must
        # follow the stamp, or `_owns_record` keeps reading the dead host.
        rec = getattr(orch, "_records", {}).get(row.id)
        if rec is not None:
            rec.host = target
            rec.status = normalized
            rec.detached = True
        log_event(log, logging.INFO, "session.adopted", session_id=row.id,
                  **{"from": hb.host, "to": target, "from_status": from_status,
                     "to_status": normalized, "self": target == self_host_name()})
    return adopted


# ── the pickup (every daemon) ─────────────────────────────────────────────────────────

async def pending_rows_here() -> list[Any]:
    """Rows adopted TO this host and not yet taken: ``adopted_from`` set, stamped here,
    non-terminal, not mid-move and not ``queued`` (a queued one is the drain's)."""
    from server.sessions.orchestrator import row_stamped_here
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(DbSession).where(
                DbSession.adopted_from.is_not(None),
                DbSession.status.notin_(INACTIVE_SESSION_STATUSES),
                DbSession.status != "queued",
                DbSession.moving_to.is_(None),
            ).order_by(DbSession.created_at))).scalars().all()
    return [r for r in rows if row_stamped_here(r.host)]


async def pickup_adopted(orch) -> int:
    """One pickup tick: take every adopted row waiting for this host. Returns how many
    were taken. Per-row failures are logged and never stop the rest."""
    if getattr(orch, "_draining", False) or getattr(orch, "_hb_fenced", False):
        return 0
    rows = await pending_rows_here()
    taken = 0
    for row in rows:
        # A row already live here (a person opened it before this tick) still goes
        # through take_adopted: nothing is started for it, but its record, holds,
        # event and marker are settled the same way.
        try:
            await take_adopted(orch, row)
            taken += 1
        except Exception:  # noqa: BLE001 — one bad row must not strand the others
            log.warning("adoption: taking session %s on this host failed", row.id,
                        exc_info=True)
    return taken


async def _from_status(session_id: str, from_host: str) -> str | None:
    """The status the session was interrupted in, from the pass's ``interrupted`` event
    (newest ``host_lost`` entry for that host)."""
    try:
        events = await _provenance.list_events(session_id, limit=20)
    except Exception:
        log.debug("adoption: event read failed for %s", session_id, exc_info=True)
        return None
    for ev in events:
        d = ev.get("detail") or {}
        if (ev.get("kind") == _provenance.INTERRUPTED
                and d.get("kind") == _provenance.INTERRUPTED_HOST_LOST
                and (d.get("host") in (None, from_host))):
            return d.get("fromStatus")
    return None


async def _clear_marker(session_id: str, from_host: str) -> None:
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(
            DbSession.id == session_id, DbSession.adopted_from == from_host)
            .values(adopted_from=None))


async def _set_status(session_id: str, status: str) -> None:
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                         .values(status=status))


async def _transcript_present(orch, rec) -> bool:
    """Is the conversation resumable HERE? Claude: its transcript file is in the
    per-account config dir (one shared tree across hosts). Codex: a thread lives in the
    dead host's local ``CODEX_HOME`` and cannot be resumed elsewhere. A lookup that
    raises answers True: ``ensure_driver``'s own fallback then re-checks the file and
    only starts fresh once it is proven gone — a flaky mount must never orphan a
    transcript (the 2026-09-04 lesson in that fallback). Off the event loop: the glob
    fallback walks ~1700 project dirs, over sshfs on a worker (the 2026-08-21 outage)."""
    if rec.engine is not AgentEngine.CLAUDE_CODE or not rec.engine_session_id:
        return False
    from viberpit.paths import account_cfg_dir
    try:
        return await asyncio.to_thread(
            orch._find_transcript, str(account_cfg_dir(rec.account)),
            rec.engine_session_id, rec.cwd) is not None
    except Exception:
        log.info("adoption: transcript lookup failed for %s — leaving the decision to "
                 "the resume", rec.id, exc_info=True)
        return True


async def _ensure_branch_here(orch, rec) -> None:
    """The dead host's worktree is on the dead host's disk; ``ensure_driver`` recreates
    it here from the session's branch (``_ensure_session_cwd``) — which needs that
    branch in THIS host's clone. A host that never ran the session has only origin's
    copy, so fetch it first (``worktrees.ensure_local_branch``; the clone itself is
    created on demand like every cold-host dispatch). Best-effort: a refusal here
    leaves the build to say ``worktree.missing`` with its branch named."""
    import os
    from viberpit import gitrepos, worktrees
    # A worktree's `.git` is a FILE (its gitdir pointer): exists(), not isdir().
    if not rec.repo or not rec.branch or (rec.cwd and os.path.exists(os.path.join(rec.cwd, ".git"))):
        return
    try:
        root = await orch._effective_repo_root()
        clone = await asyncio.to_thread(
            gitrepos.ensure_clone, root, rec.repo, f"https://github.com/{rec.repo}.git")
        if not await asyncio.to_thread(worktrees.ensure_local_branch, str(clone), rec.branch):
            log.info("adoption: branch %r of %s is not on origin either — the build will "
                     "refuse with worktree.missing", rec.branch, rec.id)
    except Exception:
        log.info("adoption: could not prepare branch %r for %s here", rec.branch, rec.id,
                 exc_info=True)


async def _over_capacity(orch) -> bool:
    """Does the record just installed push this host over its cap? Strictly greater:
    the installed `idle` record is already counted."""
    local_cap = await orch._local_host_cap()
    if local_cap is not None and await orch.owned_active_session_count() > local_cap:
        return True
    return orch._active_session_count() > await orch._max_active_sessions()


def continuity_prompt(rec, *, from_host: str, to_host: str, last_prompt: str | None) -> str:
    prompt = (last_prompt or "").strip() or "(none recorded)"
    if len(prompt) > LAST_PROMPT_MAX_CHARS:
        prompt = prompt[:LAST_PROMPT_MAX_CHARS] + " …"
    return CONTINUITY_PROMPT.format(
        from_host=from_host, to_host=to_host, repo=rec.repo or "(none)",
        branch=rec.branch or "(unknown)", title=rec.name or "(untitled)", prompt=prompt)


async def take_adopted(orch, row, *, from_queue: bool = False) -> str:
    """Take one adopted row on THIS host (FR-204/205). Returns the ``moved_host.resume``
    outcome. *from_queue*: the drain popped its queued entry — capacity was already
    gated there and the row reads ``queued``."""
    from server.sessions.orchestrator import (
        _RESTART_TRANSIENT_STATUSES, resume_hold_tail, self_host_name)
    from server.sessions import session_queue as sq
    me = self_host_name()
    sid = row.id
    from_host = row.adopted_from or row.moved_from or "an unreachable host"
    from_status = await _from_status(sid, from_host)
    if sid in getattr(orch, "_drivers", {}):
        # Already live HERE — a person opened it (ensure_driver loads a row stamped here
        # on attach) before this tick. Its in-memory record and any open ask are the
        # truth now; replacing the record from the row or reconciling its holds would
        # close a live card. Settle only the hand-over bookkeeping.
        await _provenance.record(sid, _provenance.MOVED_HOST, {
            "from": from_host, "to": me, "reason": _provenance.INTERRUPTED_HOST_LOST,
            "resume": RESUME_DEFERRED, "fromStatus": from_status})
        if row.adopted_from:
            await _clear_marker(sid, row.adopted_from)
        log_event(log, logging.INFO, "session.adoption_taken", session_id=sid,
                  **{"from": from_host, "to": me, "resume": RESUME_DEFERRED, "live": True})
        return RESUME_DEFERRED
    if from_queue:
        await _set_status(sid, "idle")
        row.status = "idle"
    rec = orch._install_row(row)
    rec.host = me
    was_interrupted = from_status in _RESTART_TRANSIENT_STATUSES
    rec.interrupted_by_restart = was_interrupted
    rec.interrupted_from_status = from_status if was_interrupted else None
    # The asks the dead host left open: lost on an interrupted row (banner + hold-aware
    # nudge), stale bookkeeping on a resting one (FR-305).
    await orch._reconcile_holds({sid: (was_interrupted, me)})

    present = await _transcript_present(orch, rec)
    eager = (was_interrupted and not is_assistant(rec)
             and not rec.auto_resume_opt_out and orch._resume_eligible(rec) is None)
    outcome: str
    if not eager:
        outcome = RESUME_DEFERRED
        if not present:
            # Nothing to resume from: the next prompt starts a new conversation,
            # told what it lost (a Codex thread stayed in the dead host's CODEX_HOME).
            rec.engine_session_id = ""
        orch._queue_prompt_preamble(sid, MOVED_NOTICE.format(
            from_host=from_host, to_host=me) + (
            "" if present else MOVED_NOTICE_NO_TRANSCRIPT.format(
                from_host=from_host, branch=rec.branch or "(unknown)")))
    elif not from_queue and await _over_capacity(orch):
        # FR-205: at cap → `queued`, with its provenance intact; the drain takes it
        # back through here (from_queue=True) when a slot frees. The marker stays
        # until then (the pickup skips queued rows; the drain needs it).
        engine = rec.engine.value if isinstance(rec.engine, AgentEngine) else str(rec.engine)
        await sq.enqueue(sid, {
            "queue_reason": QUEUE_REASON, ADOPTED_RESUME_PARAM: True,
            "name": rec.name, "account": rec.account or None, "engine": engine,
            "model": getattr(rec, "model", None), "repo": rec.repo})
        await _set_status(sid, "queued")
        rec.status = "queued"
        outcome = RESUME_QUEUED
    else:
        if not present:
            rec.engine_session_id = ""   # ensure_driver starts a new conversation
        await _ensure_branch_here(orch, rec)
        driver = None
        try:
            driver = await orch.ensure_driver(sid, SYSTEM_IDENTITY)
        except Exception:
            log.warning("adoption: resume of session %s on %s failed", sid, me,
                        exc_info=True)
        if driver is None:
            # A refused build (lease still held by a twin, worktree not recreatable, a
            # transient fault): the capped retry every dropped session gets
            # (`_recover_resume_failures_once`, with its gave-up banner) owns it from
            # here — the marker is cleared below so the pickup does not hammer it.
            outcome = RESUME_FAILED
            if hasattr(orch, "_resume_recovery"):
                orch._resume_recovery.setdefault(sid, 0)
        else:
            if present:
                text = moved_resume_nudge(from_host=from_host, to_host=me)
            else:
                text = continuity_prompt(rec, from_host=from_host, to_host=me,
                                         last_prompt=getattr(row, "initial_prompt", None))
            text += resume_hold_tail(rec)
            sent = await orch.send_prompt(sid, text, replayable=False)
            outcome = ((RESUME_TRANSCRIPT if present else RESUME_FRESH)
                       if sent == "sent" else RESUME_FAILED)
            if sent != "sent":
                log.warning("adoption: continuity prompt for %s not delivered: %s", sid, sent)
    await _provenance.record(sid, _provenance.MOVED_HOST, {
        "from": from_host, "to": me, "reason": _provenance.INTERRUPTED_HOST_LOST,
        "resume": outcome, "fromStatus": from_status})
    if outcome != RESUME_QUEUED and row.adopted_from:
        await _clear_marker(sid, row.adopted_from)
    log_event(log, logging.INFO, "session.adoption_taken", session_id=sid,
              **{"from": from_host, "to": me, "resume": outcome})
    return outcome
