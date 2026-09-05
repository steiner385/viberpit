"""Issue #434: a session whose in-flight turn was dropped by a daemon restart must be
labelled ``interruptedByRestart`` so the dashboard can say "re-send to resume" instead of
rendering a generic idle/"Turn failed" card.

The signal already exists implicitly: ``rehydrate()`` normalizes a mid-turn status
(working/in_progress/condensing) to ``idle`` on daemon boot — that normalization *is* the
"a restart dropped this turn" event. These tests pin the flag that captures it and its
round-trip through both serializers (IPC ``to_dict``/``from_dict`` and HTTP
``_record_to_dict``).
"""
import asyncio
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "restart@example.com"}


def test_record_roundtrip_preserves_interrupted_flag():
    rec = SessionRecord(
        id="s1", name="S1", account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c", cols=80, rows=24,
        cwd="/tmp", interrupted_by_restart=True,
    )
    assert rec.to_dict()["interruptedByRestart"] is True
    assert SessionRecord.from_dict(rec.to_dict()).interrupted_by_restart is True


def test_record_defaults_interrupted_flag_false():
    rec = SessionRecord(
        id="s2", name="S2", account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c", cols=80, rows=24, cwd="/tmp",
    )
    assert rec.interrupted_by_restart is False
    assert rec.to_dict()["interruptedByRestart"] is False
    # A dict from before the field existed still deserializes (defaults to False).
    legacy = rec.to_dict()
    del legacy["interruptedByRestart"]
    assert SessionRecord.from_dict(legacy).interrupted_by_restart is False


def _seed_and_list(tmp_path, *, status: str) -> dict:
    db_url = f"sqlite+aiosqlite:///{tmp_path / f'ccswitch-{status}.db'}"

    async def seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="sess", name="Sess", account_name="acct",
                             engine_session_id="c", status=status, cwd=str(tmp_path)))
    asyncio.run(seed())

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        return {s["id"]: s for s in resp.json()}["sess"]


def test_rehydrated_midturn_session_is_flagged_interrupted(tmp_path):
    for status in ("working", "in_progress", "condensing"):
        sess = _seed_and_list(tmp_path, status=status)
        assert sess["status"] == "idle", status
        assert sess["interruptedByRestart"] is True, status


def test_rehydrated_idle_session_is_not_flagged(tmp_path):
    sess = _seed_and_list(tmp_path, status="idle")
    assert sess["status"] == "idle"
    assert sess["interruptedByRestart"] is False
