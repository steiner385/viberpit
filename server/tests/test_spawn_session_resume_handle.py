"""#1107: _spawn_session must resume an existing conversation, never silently
replace it with a fresh one.

`_maybe_start_queued` drains a 'queued' row straight into `_spawn_session`
with `preexisting_row=True`. If that row already carries a `engine_session_id`
(a previously-run session whose row ended up back in the queue) and the call
site does not thread it through, `_spawn_session` used to build its
ClaudeAgentDriver with no resume handle at all: the SDK starts a brand-new
conversation, and `on_session_id` overwrites the row's pointer to the old
one, permanently orphaning the real transcript still sitting on disk. This
is the sibling of #1100 (an undelivered prompt, rather than a whole
conversation, discarded the same way).
"""
from __future__ import annotations

import asyncio
import logging

from server.db.models import Session as DbSession
from server.sessions.engine import AgentEngine


def _params(**over) -> dict:
    p = {
        "name": "resumed one", "account": "acct", "initial_prompt": None,
        "cols": 80, "rows": 24, "cwd": "/tmp/q", "branch": None, "repo": None,
        "identity": "system", "permission_mode": "interactive",
        "attachment_ids": None, "name_is_auto": True,
        "origin": None, "origin_ref": None, "model": None,
    }
    p.update(over)
    return p


async def _seed_queued_with_conversation(session_id: str, engine_session_id: str,
                                          **over) -> None:
    """A 'queued' row that already ran a turn — the shape a re-queued session
    that lost a rotation race would have, as opposed to a genuinely-fresh one
    (whose engine_session_id is NULL, see test_orphan_initial_prompt.py)."""
    from server.db import db_session
    from server.sessions import session_queue as sq
    p = _params(**over)
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name=p["name"], account_name=p["account"] or "",
            engine_session_id=engine_session_id, status="queued", pid=None,
            cwd=p["cwd"] or "", repo=p["repo"], branch=None,
            permission_mode=p["permission_mode"], name_is_auto=p["name_is_auto"],
            origin=p["origin"], origin_ref=p["origin_ref"], model=p["model"]))
    await sq.enqueue(session_id, p)


def _orch_with_queued_record(session_id: str, engine_session_id: str, **over):
    """A bare orchestrator holding the in-memory 'queued' pre-record the drain
    requires, already carrying the prior conversation's engine_session_id."""
    from datetime import datetime

    from server.sessions.orchestrator import SessionOrchestrator, SessionRecord
    p = _params(**over)
    orch = SessionOrchestrator()
    now = datetime.utcnow()
    orch._records[session_id] = SessionRecord(
        id=session_id, name=p["name"], account=p["account"] or "",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=engine_session_id, cols=p["cols"], rows=p["rows"],
        cwd=p["cwd"] or "", repo=p["repo"], branch=None, status="queued",
        name_is_auto=True, created_at=now, updated_at=now, origin=p["origin"],
        origin_ref=p["origin_ref"], model=None)
    return orch


def test_queue_drain_threads_the_rows_engine_session_id_as_resume(tmp_path):
    """Primary fix: the call site in `_maybe_start_queued` must pass the row's
    own `engine_session_id` through as `resume`, not rely on `_spawn_session`
    to reconstruct it after the fact."""
    async def run():
        from server.db import init_db
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
        await _seed_queued_with_conversation("resume-1", "existing-csid-123")
        orch = _orch_with_queued_record("resume-1", "existing-csid-123")
        seen: dict = {}

        async def _spawn(sid, **kwargs):
            seen.update(kwargs)

        orch._spawn_session = _spawn  # type: ignore[method-assign]
        await orch._maybe_start_queued()

        assert seen.get("preexisting_row") is True
        assert seen.get("resume") == "existing-csid-123", (
            "queue-drain must thread the row's engine_session_id through as "
            "resume, or _spawn_session will start a brand-new conversation "
            "over the existing one")

    asyncio.run(run())


def test_queue_drain_passes_none_for_a_genuinely_fresh_row(tmp_path):
    """A row that never ran (engine_session_id NULL) must not be handed a
    resume handle — fresh spawn is correct there."""
    async def run():
        from server.db import db_session, init_db
        from server.sessions import session_queue as sq
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
        p = _params()
        async with db_session() as db:
            db.add(DbSession(
                id="fresh-1", name=p["name"], account_name=p["account"] or "",
                engine_session_id=None, status="queued", pid=None,
                cwd=p["cwd"] or "", repo=p["repo"], branch=None,
                permission_mode=p["permission_mode"], name_is_auto=p["name_is_auto"],
                origin=p["origin"], origin_ref=p["origin_ref"], model=p["model"]))
        await sq.enqueue("fresh-1", p)
        orch = _orch_with_queued_record("fresh-1", "")
        seen: dict = {}

        async def _spawn(sid, **kwargs):
            seen.update(kwargs)

        orch._spawn_session = _spawn  # type: ignore[method-assign]
        await orch._maybe_start_queued()

        assert seen.get("resume") is None

    asyncio.run(run())


def test_spawn_session_falls_back_to_the_prior_records_id_and_warns(tmp_path, caplog):
    """Defense in depth: even if some future caller passes preexisting_row=True
    with no resume handle, `_spawn_session` must not silently discard a
    conversation the in-memory record already knows about — and must log the
    near-miss with both the session id and the discarded engine_session_id so
    the orphaned transcript can still be found."""
    async def run():
        from datetime import datetime

        from server.db import db_session
        from server.sessions.orchestrator import SessionRecord
        from server.tests.test_session_queue_drain import _make_orch, _stop_orch

        db_url = f"sqlite+aiosqlite:///{tmp_path / 'fallback.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=5)
        try:
            sid = "fallback-1"
            cwd = str(tmp_path / "cwd")
            import os
            os.makedirs(cwd, exist_ok=True)
            async with db_session() as db:
                db.add(DbSession(
                    id=sid, name="n", account_name="alpha",
                    engine_session_id="orphaned-csid-999", status="idle", pid=None,
                    cwd=cwd, repo=None, branch=None,
                    permission_mode="interactive", name_is_auto=True,
                    origin=None, origin_ref=None, model=None))
            now = datetime.utcnow()
            orch._records[sid] = SessionRecord(
                id=sid, name="n", account="alpha",
                engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="orphaned-csid-999",
                cols=80, rows=24, cwd=cwd, repo=None, branch=None,
                status="queued", name_is_auto=True, created_at=now, updated_at=now)

            resumes_seen: list = []
            base_factory = orch._sdk_factory

            def _spy_factory(cwd, config_dir, can_use_tool, mode, resume, **kw):
                resumes_seen.append(resume)
                return base_factory(cwd, config_dir, can_use_tool, mode, resume, **kw)

            orch._sdk_factory = _spy_factory

            with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
                await orch._spawn_session(
                    sid, name="n", account="alpha", initial_prompt=None,
                    cols=80, rows=24, cwd=cwd, branch=None, repo=None,
                    identity="system", permission_mode="interactive",
                    attachment_ids=None, name_is_auto=True, origin=None,
                    origin_ref=None, model=None, preexisting_row=True, resume=None,
                )

            assert resumes_seen == ["orphaned-csid-999"], (
                "a resume=None spawn over a row with a known prior "
                "engine_session_id must still resume it, not start fresh")
            assert any("orphaned-csid-999" in r.message for r in caplog.records), (
                "discarding a prior conversation's id must be logged so the "
                "orphaned transcript can be found")
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
