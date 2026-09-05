"""Reconcile status='queued' session rows that have no session_queue entry.

The live fleet stranded one session at status='queued' with no matching
session_queue row: the drain loop (server/sessions/session_queue.py) iterates
the queue table, so an orphaned queued row can never be picked up and nothing
reaps it — invisible garbage that also renders in the UI as a duplicate queue
position (two cards both numbered 1).
"""
import asyncio

from server.db.models import Session as DbSession
from server.db.models import SessionQueue


async def _seed_session(db_url: str, *, session_id: str, status: str = "queued",
                        initial_prompt: str | None = "do the thing") -> None:
    from server.db import db_session, init_db
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name="orphan", account_name="acct",
            engine_session_id=None, status=status, pid=None,
            cwd="/tmp/orphan", repo=None, branch=None,
            permission_mode="interactive", name_is_auto=True,
            origin="backlog-runner", origin_ref="acme/repo#1", model=None,
            initial_prompt=initial_prompt))


def test_orphan_queued_row_is_reconciled(tmp_path):
    """A queued row with no session_queue entry is no longer stranded after reconcile."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
        # No initial_prompt → nothing to replay, so the last-resort reap applies.
        # (The replayable branch — an intact prompt on a never-run session — is
        # covered by test_queue_orphan_prompt_replay.py, #1100.)
        await _seed_session(db_url, session_id="orphan-1", initial_prompt=None)

        orch = SessionOrchestrator()
        n = await orch._reconcile_orphan_queued()
        assert n == 1

        from sqlalchemy import select
        from server.db import db_session
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "orphan-1"))).scalar_one()
            # Decision: mark ended (see _reconcile_orphan_queued docstring) — the
            # sessions row structurally lacks cols/rows/identity/attachment_ids,
            # which only ever live in the (now-gone) session_queue.params blob,
            # so a replay here would have nothing to replay.
            assert row.status == "ended"

    asyncio.run(run())


def test_healthy_queued_row_with_queue_entry_is_untouched(tmp_path):
    """A queued row WITH a matching session_queue entry must be left exactly as is."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        from server.sessions import session_queue as sq
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"
        await _seed_session(db_url, session_id="healthy-1")
        priority = await sq.enqueue("healthy-1", {
            "name": "healthy", "account": "acct", "initial_prompt": "hi",
            "cols": 80, "rows": 24, "cwd": "/tmp/healthy", "branch": None,
            "repo": None, "identity": "system", "permission_mode": "interactive",
            "attachment_ids": None, "name_is_auto": True,
            "origin": "backlog-runner", "origin_ref": "acme/repo#2", "model": None,
        })

        orch = SessionOrchestrator()
        n = await orch._reconcile_orphan_queued()
        assert n == 0

        from sqlalchemy import select
        from server.db import db_session
        async with db_session() as db:
            row = (await db.execute(
                select(DbSession).where(DbSession.id == "healthy-1"))).scalar_one()
            assert row.status == "queued"
        positions = await sq.positions()
        assert positions == {"healthy-1": 1}
        assert priority == 1

    asyncio.run(run())


def test_reconcile_is_idempotent(tmp_path):
    """Running reconcile twice must not double-enqueue or double-process a row."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
        await _seed_session(db_url, session_id="orphan-2")

        orch = SessionOrchestrator()
        first = await orch._reconcile_orphan_queued()
        second = await orch._reconcile_orphan_queued()
        assert first == 1
        assert second == 0

        from sqlalchemy import select
        from server.db import db_session
        from server.sessions import session_queue as sq
        async with db_session() as db:
            rows = (await db.execute(
                select(SessionQueue).where(SessionQueue.session_id == "orphan-2")
            )).scalars().all()
        # Never more than one queue entry for the session, regardless of remedy.
        assert len(rows) <= 1
        positions = await sq.positions()
        assert list(positions.values()).count(
            positions.get("orphan-2", -1)) <= 1

    asyncio.run(run())
