"""Mid-session auto-archive opt-out: the set_auto_archive_opt_out orchestrator method
(writes the SAME auto_archive_opt_out_reason column as dispatch-time opt-out and
manual-unarchive — see AUTO_ARCHIVE_OPT_OUT_SESSION_TOGGLE in types.py) and the
RemoteOrchestrator proxy (daemon parity)."""
from __future__ import annotations

import asyncio

from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import AUTO_ARCHIVE_OPT_OUT_SESSION_TOGGLE


def test_remote_proxy_has_set_auto_archive_opt_out():
    # Daemon parity: the request layer calls set_auto_archive_opt_out, so the remote
    # proxy must expose it (or breadth tests / daemon mode break).
    assert hasattr(RemoteOrchestrator, "set_auto_archive_opt_out")


def test_set_auto_archive_opt_out_persists(tmp_path):
    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        from sqlalchemy import select
        import server.sessions.orchestrator as orch_mod
        from server.sessions.types import SessionRecord
        from server.sessions.engine import AgentEngine

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'optout.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp"))

        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                             engine_session_id="", cols=80, rows=24, cwd="/tmp", repo=None)
        orch._records = {"s1": rec}

        await orch.set_auto_archive_opt_out("s1", True)

        # in-memory record updated ...
        assert orch._records["s1"].auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_SESSION_TOGGLE
        # ... and the DB column persisted (survives a rehydrate).
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(
                DbSession.id == "s1"))).scalar_one()
            assert row.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_SESSION_TOGGLE

        # Toggling back off clears the reason (re-enrolled in auto-archive).
        await orch.set_auto_archive_opt_out("s1", False)
        assert orch._records["s1"].auto_archive_opt_out_reason is None
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(
                DbSession.id == "s1"))).scalar_one()
            assert row.auto_archive_opt_out_reason is None

    asyncio.run(run())
