"""Automation provenance must survive every row→record rebuild.

Third instance of the same bug shape (autopilot_paused was dropped by unarchive,
origin/origin_ref by rehydrate AND unarchive): two hand-maintained parallel
SessionRecord constructions drift. These tests pin the fix — a single
_record_from_row helper — by asserting the fields survive both paths, plus the
new assisted_by marker set when autopilot first acts on a session."""
from __future__ import annotations

import asyncio

from server.automations.autopilot_policy import Decision
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    orch._turn_restart_at = {}
    return orch


async def _seed(tmp_path, dbname, **cols):
    from pathlib import Path
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
    if cols.get("status") == "archived":
        # Archived rows resolve through archive_root — seed and materialize it so
        # unarchive performs a real restore instead of hitting ArchiveMissingError.
        cols.setdefault("archive_root", str(tmp_path / "archive"))
        Path(cols["archive_root"], "s1").mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path / "wt"),
                         **cols))


def test_rehydrate_preserves_origin_and_assisted_by(tmp_path):
    async def run():
        await _seed(tmp_path, "reh.db", status="done",
                    origin="backlog-runner", origin_ref="o/r#1",
                    assisted_by="autopilot", repo="o/r",
                    inferred_goal="Ship the widget PR")
        orch = _bare_orch()
        await orch.rehydrate()
        rec = orch._records["s1"]
        assert rec.origin == "backlog-runner"       # the purple badge's source of truth
        assert rec.origin_ref == "o/r#1"
        assert rec.assisted_by == "autopilot"
        assert rec.repo == "o/r"
        assert rec.inferred_goal == "Ship the widget PR"   # row-mapped once, in _record_from_row
    asyncio.run(run())


def test_unarchive_preserves_origin_repo_and_assisted_by(tmp_path):
    async def run():
        await _seed(tmp_path, "unarch.db", status="archived",
                    origin="backlog-runner", origin_ref="o/r#2",
                    assisted_by="autopilot", repo="o/r")
        orch = _bare_orch()
        rec = await orch.unarchive("s1")
        assert rec is not None
        assert rec.origin == "backlog-runner"
        assert rec.origin_ref == "o/r#2"
        assert rec.assisted_by == "autopilot"
        assert rec.repo == "o/r"
    asyncio.run(run())


def test_session_record_roundtrips_assisted_by():
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                        cols=80, rows=24, cwd="/tmp", assisted_by="autopilot")
    d = rec.to_dict()
    assert d["assistedBy"] == "autopilot"
    assert SessionRecord.from_dict(d).assisted_by == "autopilot"


def test_record_to_dict_serializes_assisted_by():
    from server.api.sessions import _record_to_dict
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                        cols=80, rows=24, cwd="/tmp", assisted_by="autopilot")
    assert _record_to_dict(rec, repo=None)["assistedBy"] == "autopilot"


def test_autopilot_apply_marks_session_assisted(tmp_path):
    """The first successful autopilot action stamps assisted_by (record + DB)."""
    async def run():
        from server.db import db_session
        from server.db.models import Session as DbSession
        from sqlalchemy import select
        await _seed(tmp_path, "assist.db", status="done")
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(id="s1", name="n", account="a",
                                             engine=AgentEngine.CLAUDE_CODE,
                                             engine_session_id="", cols=80, rows=24,
                                             cwd="/tmp", status="done")}

        async def _ensure(sid, ident=None):
            return object()
        orch.ensure_driver = _ensure

        async def _send(sid, text, attachment_ids=None):
            return "sent"
        orch.send_prompt = _send

        from server.automations.autopilot_policy import DecisionContext
        ctx = DecisionContext(session_id="s1", status="done", account="a", repo=None,
                              branch=None, effective_goal="g", goal_source="explicit",
                              stuck={"kind": "done"}, transcript_tail=[], ledger={})
        ok = await orch._autopilot_apply("s1", Decision(action="nudge", prompt="go"), ctx)
        assert ok is True
        assert orch._records["s1"].assisted_by == "autopilot"
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.assisted_by == "autopilot"
    asyncio.run(run())
