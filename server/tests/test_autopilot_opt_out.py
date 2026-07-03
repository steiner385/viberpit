"""Per-session autopilot opt-out: the autopilot_paused column, its SessionRecord
mirror + serialization, the set_autopilot_paused orchestrator method, and the
RemoteOrchestrator proxy (daemon parity)."""
from __future__ import annotations

import asyncio

from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord


def _rec(autopilot_paused: bool = False) -> SessionRecord:
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                         cols=80, rows=24, cwd="/tmp", repo=None,
                         autopilot_paused=autopilot_paused)


def test_remote_proxy_has_set_autopilot_paused():
    # Daemon parity: the request layer calls set_autopilot_paused, so the remote
    # proxy must expose it (or breadth tests / daemon mode break).
    assert hasattr(RemoteOrchestrator, "set_autopilot_paused")


def test_session_record_roundtrips_autopilot_paused():
    d = _rec(autopilot_paused=True).to_dict()
    assert d["autopilotPaused"] is True
    assert SessionRecord.from_dict(d).autopilot_paused is True
    # default when absent from the wire payload
    d2 = _rec().to_dict()
    assert d2["autopilotPaused"] is False


def test_set_autopilot_paused_persists(tmp_path):
    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        from sqlalchemy import select
        import server.sessions.orchestrator as orch_mod

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'optout.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp"))

        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        orch._records = {"s1": _rec()}

        await orch.set_autopilot_paused("s1", True)

        # in-memory record updated ...
        assert orch._records["s1"].autopilot_paused is True
        # ... and the DB column persisted (survives a rehydrate).
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(
                DbSession.id == "s1"))).scalar_one()
            assert row.autopilot_paused is True

    asyncio.run(run())
