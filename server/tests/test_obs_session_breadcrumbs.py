"""Tests for Task 8: session lifecycle breadcrumbs in orchestrator.

Contract test: log_event emits a record with .event and .fields.
Integration tests: drive real transitions (create_session, rehydrate, unpark,
archive_session) against the existing test fixtures and assert breadcrumbs fire.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import patch

from server.obs.context import log_event


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


# ---------------------------------------------------------------------------
# Contract test — shape of a log_event breadcrumb
# ---------------------------------------------------------------------------

def test_log_event_shapes_session_start(caplog):
    """Contract test: a session.start breadcrumb carries from/to + session_id."""
    logger = logging.getLogger("server.sessions.orchestrator")
    with caplog.at_level(logging.INFO):
        log_event(logger, logging.INFO, "session.start",
                  session_id="s1", **{"from": None, "to": "starting", "reason": "create"})
    rec = _events(caplog, "session.start")[-1]
    assert rec.fields["session_id"] == "s1"
    assert rec.fields["to"] == "starting"


# ---------------------------------------------------------------------------
# Integration: session.start emitted by create_session
# ---------------------------------------------------------------------------

def test_create_session_emits_session_start(tmp_path, caplog):
    """create_session emits a session.start breadcrumb with to='idle'."""
    from unittest.mock import patch
    from ccswitch.selector import Picked
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()

    def _fake_usage(account: str):
        from datetime import datetime, timedelta, timezone
        from ccswitch.oauth import Usage
        reset = datetime.now(timezone.utc) + timedelta(hours=5)
        return Usage(10.0, reset, 10.0, reset, False)

    async def run():
        await init_db(db_url=db_url)
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), usage_fn=_fake_usage)
        with (
            patch.dict(os.environ, {
                "CCSWITCH_HOME": str(tmp_path / "store"),
                "CLAUDE_CONFIG_DIR": str(shared_base),
            }),
            patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
            patch("server.sessions.orchestrator.select_account",
                  return_value=Picked("alpha")),
            patch("ccswitch.configdir.trust_project"),
            caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"),
        ):
            rec = await orch.create_session("My session", account="alpha")
        return rec

    rec = asyncio.run(run())
    starts = _events(caplog, "session.start")
    assert len(starts) == 1
    ev = starts[0]
    assert ev.fields["session_id"] == rec.id
    assert ev.fields["to"] == "idle"
    assert ev.fields["from"] is None
    assert ev.fields["reason"] == "create"


# ---------------------------------------------------------------------------
# Integration: session.reattach emitted by rehydrate
# ---------------------------------------------------------------------------

def test_rehydrate_emits_session_reattach(tmp_path, caplog):
    """rehydrate emits a session.reattach breadcrumb per loaded session."""
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def _seed(rows):
        async with db_session() as db:
            for r in rows:
                db.add(DbSession(**r))

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a", name="Alpha", account_name="acct1",
                 engine_session_id="cs-a", status="idle", cwd="/repo/a"),
            dict(id="b", name="Beta", account_name="acct1",
                 engine_session_id=None, status="working", cwd="/repo/b"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            n = await orch.rehydrate()
        return n

    n = asyncio.run(run())
    assert n == 2
    reattaches = _events(caplog, "session.reattach")
    assert len(reattaches) == 2
    by_id = {r.fields["session_id"]: r for r in reattaches}
    assert "a" in by_id
    assert "b" in by_id
    # "working" is normalized to "idle" during rehydrate
    assert by_id["b"].fields["from"] == "working"
    assert by_id["b"].fields["to"] == "idle"
    assert by_id["a"].fields["from"] == "idle"
    assert by_id["a"].fields["to"] == "idle"
    for r in reattaches:
        assert r.fields["reason"] == "rehydrate"


# ---------------------------------------------------------------------------
# Integration: session.unpark emitted by rehydrate for cleared waiting_external
# ---------------------------------------------------------------------------

def test_rehydrate_emits_session_unpark_when_not_parked(tmp_path, caplog):
    """rehydrate emits session.unpark when a waiting_external session has no bg tasks."""
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    # Create a real (empty) transcript path so is_parked returns False
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()

    async def _seed(rows):
        async with db_session() as db:
            for r in rows:
                db.add(DbSession(**r))

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="wx1", name="WX", account_name="acct1",
                 engine_session_id=None, status="waiting_external", cwd="/repo/wx"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        # Patch transcript_path so we control what is_parked reads
        fake_transcript = str(tmp_path / "transcripts" / "wx1.jsonl")
        Path(fake_transcript).write_text("")  # empty = no bg tasks → not parked

        with patch.object(orch, "transcript_path", return_value=fake_transcript):
            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                n = await orch.rehydrate()
        return n, orch._records.get("wx1")

    n, rec = asyncio.run(run())
    assert n == 1
    # Status should be healed to "done"
    assert rec is not None
    assert rec.status == "done"
    # Breadcrumbs
    unparks = _events(caplog, "session.unpark")
    assert len(unparks) == 1
    ev = unparks[0]
    assert ev.fields["session_id"] == "wx1"
    assert ev.fields["from"] == "waiting_external"
    assert ev.fields["to"] == "done"
    assert ev.fields["reason"] == "bg_tasks_cleared"


# ---------------------------------------------------------------------------
# Integration: session.terminal emitted by archive_session
# ---------------------------------------------------------------------------

def test_archive_session_emits_session_terminal(tmp_path, caplog):
    """archive_session emits a session.terminal breadcrumb with to='archived'."""
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = tmp_path / "session_cwd"
    cwd.mkdir()

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            async with db_session() as db:
                db.add(DbSession(
                    id="s1", name="S1", account_name="acct1",
                    engine_session_id=None, status="done", cwd=str(cwd),
                ))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            orch._records["s1"] = SessionRecord(
                id="s1", name="S1", account="acct1",
                engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="", cols=80, rows=24, cwd=str(cwd),
                status="done",
            )
            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                await orch.archive_session("s1")

    asyncio.run(run())
    terminals = _events(caplog, "session.terminal")
    assert len(terminals) == 1
    ev = terminals[0]
    assert ev.fields["session_id"] == "s1"
    assert ev.fields["from"] == "done"
    assert ev.fields["to"] == "archived"
    assert ev.fields["reason"] == "archive"


# ---------------------------------------------------------------------------
# Contract: session.rotate shape (integration against _reassign is heavy;
# covered by contract + a concern note in the report)
# ---------------------------------------------------------------------------

def test_log_event_shapes_session_rotate(caplog):
    """Contract test: session.rotate breadcrumb carries from_account/to_account."""
    logger = logging.getLogger("server.sessions.orchestrator")
    with caplog.at_level(logging.INFO):
        log_event(logger, logging.INFO, "session.rotate",
                  session_id="s2", from_account="acct-a", to_account="acct-b",
                  reason="session limit reached")
    rec = _events(caplog, "session.rotate")[-1]
    assert rec.fields["session_id"] == "s2"
    assert rec.fields["from_account"] == "acct-a"
    assert rec.fields["to_account"] == "acct-b"
    assert rec.fields["reason"] == "session limit reached"
