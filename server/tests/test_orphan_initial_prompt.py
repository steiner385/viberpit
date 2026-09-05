"""No session may survive a daemon restart with its initial prompt undelivered.

Root cause (RCCA, issue: "queued/just-started sessions come back as dead orphans"):
initial-prompt delivery had no durable, row-backed state. Its liveness lived in two
process-local places, so any daemon restart between "session row created" and "first
turn started" stranded the session permanently:

  1. ``_maybe_start_queued`` deleted the ``session_queue`` entry BEFORE calling
     ``_spawn_session``. A daemon death in that window left a ``sessions`` row at
     'queued' with no queue entry, and ``_reconcile_orphan_queued`` then marked it
     'ended' — silently discarding the work, because the spawn params live ONLY in
     ``session_queue.params`` and had just been deleted.

  2. ``_pending_initial_prompt`` is an in-memory dict that ``rehydrate()`` never
     re-stages, so a spawned-but-never-attached session lost its prompt forever and
     rested at 'idle' with an empty conversation.

These tests pin both windows shut: a crash mid-spawn must RETRY (entry survives), and
a rehydrated session that never ran a turn must get its prompt back.
"""
from __future__ import annotations

import asyncio

from server.db.models import Session as DbSession
from server.db.models import SessionQueue
from server.sessions.engine import AgentEngine


async def _init(db_url: str) -> None:
    from server.db import init_db
    await init_db(db_url=db_url)


def _params(**over) -> dict:
    p = {
        "name": "queued one", "account": "acct", "initial_prompt": "do the thing",
        "cols": 80, "rows": 24, "cwd": "/tmp/q", "branch": None, "repo": None,
        "identity": "system", "permission_mode": "interactive",
        "attachment_ids": None, "name_is_auto": True,
        "origin": "backlog-runner", "origin_ref": "acme/repo#1", "model": None,
    }
    p.update(over)
    return p


async def _seed_queued(session_id: str, **over) -> None:
    """Persist a 'queued' sessions row + its session_queue entry, as _enqueue_session does."""
    from server.db import db_session
    from server.sessions import session_queue as sq
    p = _params(**over)
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name=p["name"], account_name=p["account"] or "",
            engine_session_id=None, status="queued", pid=None,
            cwd=p["cwd"] or "", repo=p["repo"], branch=None,
            permission_mode=p["permission_mode"], name_is_auto=p["name_is_auto"],
            origin=p["origin"], origin_ref=p["origin_ref"], model=p["model"],
            initial_prompt=p["initial_prompt"]))
    await sq.enqueue(session_id, p)


async def _settle() -> None:
    """Let the tracked redelivery tasks rehydrate() scheduled actually run."""
    for _ in range(10):
        await asyncio.sleep(0)


def _orch_with_queued_record(session_id: str, **over):
    """A bare orchestrator holding the in-memory 'queued' pre-record the drain requires."""
    from datetime import datetime

    from server.sessions.orchestrator import SessionOrchestrator, SessionRecord
    p = _params(**over)
    orch = SessionOrchestrator()
    now = datetime.utcnow()
    orch._records[session_id] = SessionRecord(
        id=session_id, name=p["name"], account=p["account"] or "",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="", cols=p["cols"], rows=p["rows"], cwd=p["cwd"] or "",
        repo=p["repo"], branch=None, status="queued", name_is_auto=True,
        created_at=now, updated_at=now, origin=p["origin"],
        origin_ref=p["origin_ref"], model=None)
    return orch


# ---------------------------------------------------------------------------
# Window 1: the queue entry must outlive the spawn, so a crash retries.
# ---------------------------------------------------------------------------

def test_queue_entry_survives_a_spawn_crash_so_the_next_boot_retries(tmp_path):
    """A daemon killed mid-``_spawn_session`` must leave the queue entry intact.

    CancelledError stands in for the process dying mid-spawn (SIGTERM drain). The
    entry is the ONLY place the spawn params live, so losing it is what forced
    ``_reconcile_orphan_queued`` to discard the session as 'ended'.
    """
    async def run():
        from server.sessions import session_queue as sq
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'crash.db'}")
        await _seed_queued("crash-1")
        orch = _orch_with_queued_record("crash-1")

        async def _die(*a, **k):
            raise asyncio.CancelledError()

        orch._spawn_session = _die  # type: ignore[method-assign]
        try:
            await orch._maybe_start_queued()
        except asyncio.CancelledError:
            pass

        assert await sq.positions() == {"crash-1": 1}, (
            "queue entry was consumed before the spawn committed — the session's "
            "spawn params are gone and the next boot can only discard it")

    asyncio.run(run())


def test_queue_entry_is_still_present_while_the_spawn_runs(tmp_path):
    """Ordering guard: the entry is removed only AFTER _spawn_session returns."""
    async def run():
        from server.sessions import session_queue as sq
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'order.db'}")
        await _seed_queued("order-1")
        orch = _orch_with_queued_record("order-1")
        seen: dict[str, dict] = {}

        async def _spawn(sid, **kwargs):
            seen["during"] = await sq.positions()
            return None

        orch._spawn_session = _spawn  # type: ignore[method-assign]
        await orch._maybe_start_queued()

        assert seen["during"] == {"order-1": 1}, "entry vanished before the spawn committed"
        assert await sq.positions() == {}, "entry must be dropped once the spawn commits"

    asyncio.run(run())


def test_failed_spawn_still_drops_the_entry_and_ends_the_session(tmp_path):
    """A genuine (non-crash) spawn failure must not leave the queue wedged forever."""
    async def run():
        from sqlalchemy import select

        from server.db import db_session
        from server.sessions import session_queue as sq
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}")
        await _seed_queued("fail-1")
        orch = _orch_with_queued_record("fail-1")

        async def _boom(*a, **k):
            raise RuntimeError("boom")

        orch._spawn_session = _boom  # type: ignore[method-assign]
        await orch._maybe_start_queued()

        assert await sq.positions() == {}, "a failed start must leave the queue"
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "fail-1"))).scalar_one()
        assert row.status == "ended"

    asyncio.run(run())


def test_stale_entry_whose_row_left_queued_is_dropped(tmp_path):
    """An entry for a session no longer 'queued' is removed, never re-spawned.

    Now that the entry outlives the spawn, a crash AFTER the spawn committed leaves
    an entry pointing at an already-started session; the drain must reap it.
    """
    async def run():
        from server.sessions import session_queue as sq
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
        await _seed_queued("stale-1")
        orch = _orch_with_queued_record("stale-1")
        orch._records["stale-1"].status = "idle"  # spawn already committed
        spawned: list[str] = []

        async def _spawn(sid, **kwargs):
            spawned.append(sid)

        orch._spawn_session = _spawn  # type: ignore[method-assign]
        await orch._maybe_start_queued()

        assert spawned == [], "a session that already started must never be re-spawned"
        assert await sq.positions() == {}, "the stale entry must be reaped"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Window 2: rehydrate must recover an undelivered initial prompt.
# ---------------------------------------------------------------------------

async def _seed_spawned(session_id: str, *, origin, engine_session_id=None,
                        status="idle", initial_prompt="do the thing") -> None:
    from server.db import db_session
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name="spawned", account_name="acct",
            engine_session_id=engine_session_id, status=status, pid=None,
            cwd=str("/tmp"), repo=None, branch=None,
            permission_mode="interactive", name_is_auto=True,
            origin=origin, origin_ref=None, model=None,
            initial_prompt=initial_prompt))


def test_rehydrate_restages_an_undelivered_human_prompt(tmp_path):
    """origin=None: the prompt is re-staged for the next WS attach, as at spawn time."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
        await _seed_spawned("human-1", origin=None)

        orch = SessionOrchestrator()
        await orch.rehydrate()

        assert orch.pop_pending_initial_prompt("human-1") == "do the thing", (
            "a human session that was spawned but never attached lost its prompt "
            "to the restart — it will sit at 'Ready' with an empty conversation forever")

    asyncio.run(run())


def test_rehydrate_redelivers_an_undelivered_automation_prompt(tmp_path):
    """origin set: no human will ever attach, so the prompt must be re-sent now."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
        await _seed_spawned("auto-1", origin="backlog-runner")

        orch = SessionOrchestrator()
        sent: list[tuple] = []

        async def _send(sid, prompt, attachment_ids=None):
            sent.append((sid, prompt))

        async def _driver(sid):
            return object()  # a rehydrated record is detached; recovery must spawn one

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]
        await orch.rehydrate()
        await _settle()

        assert sent == [("auto-1", "do the thing")], (
            "an automation session that never ran turn one was left idle forever")

    asyncio.run(run())


def test_rehydrate_never_redelivers_to_a_session_that_already_ran(tmp_path):
    """A non-NULL engine_session_id proves turn one ran — re-sending would duplicate work."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        await _seed_spawned("ran-1", origin="backlog-runner",
                            engine_session_id="cs-123")
        await _seed_spawned("ran-2", origin=None, engine_session_id="cs-456")

        orch = SessionOrchestrator()
        sent: list[tuple] = []

        async def _send(sid, prompt, attachment_ids=None):
            sent.append((sid, prompt))

        async def _driver(sid):
            return object()  # a rehydrated record is detached; recovery must spawn one

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]
        await orch.rehydrate()
        await _settle()

        assert sent == []
        assert orch.pop_pending_initial_prompt("ran-2") is None

    asyncio.run(run())


def test_rehydrate_leaves_parked_and_queued_rows_alone(tmp_path):
    """pending_approval must stay parked; 'queued' is the drain's job, not redelivery."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        await _seed_spawned("park-1", origin="backlog-runner",
                            status="pending_approval")
        await _seed_queued("queued-1")

        orch = SessionOrchestrator()
        sent: list[tuple] = []

        async def _send(sid, prompt, attachment_ids=None):
            sent.append((sid, prompt))

        async def _driver(sid):
            return object()  # a rehydrated record is detached; recovery must spawn one

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]
        await orch.rehydrate()
        await _settle()

        assert sent == []
        assert orch.pop_pending_initial_prompt("park-1") is None
        assert orch.pop_pending_initial_prompt("queued-1") is None

    asyncio.run(run())


def test_queued_row_with_a_live_queue_entry_is_no_longer_reconciled_away(tmp_path):
    """The crash-window row now KEEPS its entry, so reconcile must leave it to the drain."""
    async def run():
        from sqlalchemy import select

        from server.db import db_session
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
        await _seed_queued("keep-1")

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 0
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "keep-1"))).scalar_one()
        assert row.status == "queued"
        async with db_session() as db:
            entries = (await db.execute(
                select(SessionQueue).where(
                    SessionQueue.session_id == "keep-1"))).scalars().all()
        assert len(entries) == 1

    asyncio.run(run())


def test_rehydrate_never_redelivers_to_a_restart_interrupted_turn(tmp_path):
    """A mid-turn row proves the prompt WAS delivered — replay belongs to #434/#864.

    rehydrate normalizes a pre-crash 'in_progress' to 'idle', which would otherwise
    look exactly like an undelivered-prompt orphan. Sending here as well would double
    up with restore_deploy's snapshotted _last_prompt replay on a graceful deploy.
    """
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
        # No engine_session_id: turn one was in flight but the SDK had not yet
        # assigned an id — the exact window where the two paths could collide.
        await _seed_spawned("mid-1", origin="backlog-runner", status="in_progress")
        await _seed_spawned("mid-2", origin=None, status="waiting_question")

        orch = SessionOrchestrator()
        sent: list[tuple] = []

        async def _send(sid, prompt, attachment_ids=None):
            sent.append((sid, prompt))

        async def _driver(sid):
            return object()

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]
        await orch.rehydrate()
        await _settle()

        assert orch._records["mid-1"].status == "idle"  # normalized, as rehydrate does
        assert sent == [], "double-sent a turn that restore_deploy also replays"
        assert orch.pop_pending_initial_prompt("mid-2") is None

    asyncio.run(run())


def test_rehydrate_never_recovers_a_row_another_host_owns(tmp_path):
    """Fleet ownership (#852): the primary holds EVERY host's rows for the dashboard.

    Recovery is an ACTING loop, so without the _owns_record gate the primary would
    re-drive turn one for a session a worker owns — while that worker drives it too.
    """
    async def run():
        from unittest.mock import patch

        from sqlalchemy import update as sa_update

        from server.db import db_session
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'own.db'}")
        await _seed_spawned("mine-1", origin="backlog-runner")
        await _seed_spawned("theirs-1", origin="backlog-runner")
        async with db_session() as db:
            await db.execute(sa_update(DbSession)
                             .where(DbSession.id == "theirs-1").values(host="worker-2"))

        orch = SessionOrchestrator()
        sent: list[tuple] = []

        async def _send(sid, prompt, attachment_ids=None):
            sent.append((sid, prompt))

        async def _driver(sid):
            return object()

        async def _hosts(self=None):
            return frozenset({"primary-1", "worker-2"})

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]
        orch._enabled_host_names = _hosts  # type: ignore[method-assign]
        with patch("server.sessions.orchestrator.self_host_name", return_value="primary-1"):
            await orch.rehydrate()
            await _settle()

        assert [s[0] for s in sent] == ["mine-1"], (
            "recovered a session owned by another live daemon — both would drive it")

    asyncio.run(run())
